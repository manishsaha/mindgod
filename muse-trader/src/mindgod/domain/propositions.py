"""Canonical outcomes: the venue-independent questions every price maps onto.

A Quantity is a random variable ("Judge total bases, full game"). An Outcome is
a condition on it ("at least 2"). Moneylines, spreads, totals, and props all
reduce to this one shape, so cross-venue matching and consistency checks
(ladders, complements, pushes) become plain arithmetic.

Canonical form: every real-world outcome has exactly one representation, so
structural equality means "same outcome".
  * MARGIN is always home minus away. Away-side bets negate the condition.
  * Numeric stats are integers, so conditions are stored as integer bounds:
    "over 3.5" and "more than 3" are both ">= 4".

Adapters must build outcomes with the builder functions at the bottom of this
module rather than assembling Quantities by hand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import assert_never

from mindgod.domain.sports import (
    PERIODS_BY_LEAGUE,
    Event,
    EventId,
    League,
    Period,
    PlayerId,
    TeamId,
)
from mindgod.domain.stats import STATS_BY_LEAGUE, Stat


@dataclass(frozen=True, slots=True)
class Quantity:
    """A random variable a bet is written on."""

    league: League
    event_id: EventId
    stat: Stat
    period: Period = Period.FULL_GAME
    team_id: TeamId | None = None  # None: game level (game total, margin)
    player_id: PlayerId | None = None

    def __post_init__(self) -> None:
        if self.stat not in STATS_BY_LEAGUE[self.league]:
            raise ValueError(f"{self.stat} is not a {self.league} stat")
        if self.period not in PERIODS_BY_LEAGUE[self.league]:
            raise ValueError(f"{self.period} is not a {self.league} period")
        if self.stat is Stat.MARGIN and self.team_id is not None:
            raise ValueError("margin is always home minus away; negate the condition instead")
        if self.player_id is not None and self.team_id is None:
            raise ValueError("a player quantity must name the player's team in this game")


# --- Conditions -----------------------------------------------------------


class Comparator(StrEnum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="


_COMPLEMENT = {
    Comparator.GT: Comparator.LTE,
    Comparator.GTE: Comparator.LT,
    Comparator.LT: Comparator.GTE,
    Comparator.LTE: Comparator.GT,
}
_MIRROR = {
    Comparator.GT: Comparator.LT,
    Comparator.GTE: Comparator.LTE,
    Comparator.LT: Comparator.GT,
    Comparator.LTE: Comparator.GTE,
    Comparator.EQ: Comparator.EQ,
}


@dataclass(frozen=True, slots=True)
class Interval:
    """A set of real numbers. None bounds are infinite."""

    lower: Decimal | None
    upper: Decimal | None
    lower_closed: bool = False
    upper_closed: bool = False

    def includes(self, other: Interval) -> bool:
        """True when every point of `other` lies inside this interval."""
        return self._covers_lower_end_of(other) and self._covers_upper_end_of(other)

    def _covers_lower_end_of(self, other: Interval) -> bool:
        if self.lower is None:
            return True
        if other.lower is None:
            return False
        if other.lower != self.lower:
            return other.lower > self.lower
        return self.lower_closed or not other.lower_closed

    def _covers_upper_end_of(self, other: Interval) -> bool:
        if self.upper is None:
            return True
        if other.upper is None:
            return False
        if other.upper != self.upper:
            return other.upper < self.upper
        return self.upper_closed or not other.upper_closed


@dataclass(frozen=True, slots=True)
class Threshold:
    """X compared against a line, e.g. X > 8.5."""

    comparator: Comparator
    line: Decimal

    def as_interval(self) -> Interval:
        match self.comparator:
            case Comparator.GT:
                return Interval(self.line, None)
            case Comparator.GTE:
                return Interval(self.line, None, lower_closed=True)
            case Comparator.LT:
                return Interval(None, self.line)
            case Comparator.LTE:
                return Interval(None, self.line, upper_closed=True)
            case Comparator.EQ:
                return Interval(self.line, self.line, lower_closed=True, upper_closed=True)
            case _:
                assert_never(self.comparator)

    def complement(self) -> Threshold | None:
        opposite = _COMPLEMENT.get(self.comparator)
        return None if opposite is None else Threshold(opposite, self.line)

    def negated(self) -> Threshold:
        """The same condition expressed on -X instead of X."""
        return Threshold(_MIRROR[self.comparator], -self.line)


@dataclass(frozen=True, slots=True)
class Band:
    """Inclusive range, e.g. 'wins by 1 to 6'. None means unbounded on that side."""

    lower: Decimal | None
    upper: Decimal | None

    def __post_init__(self) -> None:
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError(f"empty band [{self.lower}, {self.upper}]")

    def as_interval(self) -> Interval:
        return Interval(self.lower, self.upper, self.lower is not None, self.upper is not None)

    def complement(self) -> None:
        return None  # two rays: not expressible as one condition

    def negated(self) -> Band:
        return Band(_negate(self.upper), _negate(self.lower))


@dataclass(frozen=True, slots=True)
class CategoryIs:
    """For categorical stats: the result is this participant."""

    value: str

    def complement(self) -> None:
        return None


NumericCondition = Threshold | Band
Condition = Threshold | Band | CategoryIs


# --- Outcome --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """A yes/no question about a Quantity: the unit every venue's price maps onto."""

    quantity: Quantity
    condition: Condition

    def __post_init__(self) -> None:
        if self.quantity.stat.is_categorical:
            if not isinstance(self.condition, CategoryIs):
                raise ValueError(f"{self.quantity.stat} needs a CategoryIs condition")
            return
        if isinstance(self.condition, CategoryIs):
            raise ValueError(f"{self.quantity.stat} is numeric")
        # Canonicalise once, at construction, so equality means "same outcome".
        object.__setattr__(self, "condition", _integer_normal_form(self.quantity, self.condition))

    def complement(self) -> Outcome | None:
        """The outcome that happens exactly when this one does not, if it is one condition."""
        opposite = self.condition.complement()
        return None if opposite is None else Outcome(self.quantity, opposite)

    def implies(self, other: Outcome) -> bool:
        """True when this outcome guarantees `other`, so P(self) <= P(other) must hold.

        Drives ladder checks: "over 9.5 runs" implies "over 7.5 runs".
        """
        if self.quantity != other.quantity:
            return False
        if isinstance(self.condition, CategoryIs) or isinstance(other.condition, CategoryIs):
            return self.condition == other.condition
        return other.condition.as_interval().includes(self.condition.as_interval())


def _integer_normal_form(quantity: Quantity, condition: NumericCondition) -> NumericCondition:
    """Rewrite a condition on an integer-valued stat into its unique canonical form."""
    interval = condition.as_interval()
    lower = None if interval.lower is None else _smallest_int(interval.lower, interval.lower_closed)
    upper = None if interval.upper is None else _largest_int(interval.upper, interval.upper_closed)
    if upper is None:
        if lower is None:
            raise ValueError("condition is always true")
        result: NumericCondition = Threshold(Comparator.GTE, lower)
    elif lower is None:
        result = Threshold(Comparator.LTE, upper)
    elif lower > upper:
        raise ValueError(f"no integer satisfies {condition}")
    elif lower == upper:
        result = Threshold(Comparator.EQ, lower)
    else:
        result = Band(lower, upper)
    return _no_tie_normal_form(quantity, result)


def _no_tie_normal_form(quantity: Quantity, condition: NumericCondition) -> NumericCondition:
    """Exclude margin 0 where it is impossible: MLB full games cannot end tied.

    Without this, the complement of "NYY wins" ("home margin >= 1") is
    "home margin <= 0", but the book's "BOS wins" is stored as
    "home margin <= -1". The canonical forms differ, the pair never forms,
    and MLB moneyline closing lines silently go missing. Live pricing is
    unaffected (it derives No from Yes); only complement-based pairing breaks.

    Not applied to FIRST_FIVE_INNINGS or FIRST_INNING, which can tie, nor to
    the NFL, where ties are possible.
    """
    if (
        quantity.league != League.MLB
        or quantity.period != Period.FULL_GAME
        or quantity.stat != Stat.MARGIN
        or not isinstance(condition, Threshold)
        or condition.line != 0
    ):
        return condition
    if condition.comparator == Comparator.LTE:
        return Threshold(Comparator.LTE, Decimal(-1))
    if condition.comparator == Comparator.GTE:
        return Threshold(Comparator.GTE, Decimal(1))
    if condition.comparator == Comparator.EQ:
        raise ValueError("margin 0 is impossible in an MLB full game")
    return condition


def _smallest_int(bound: Decimal, closed: bool) -> Decimal:
    """Smallest integer above `bound`, or equal to it when the bound is closed."""
    return Decimal(math.ceil(bound) if closed else math.floor(bound) + 1)


def _largest_int(bound: Decimal, closed: bool) -> Decimal:
    """Largest integer below `bound`, or equal to it when the bound is closed."""
    return Decimal(math.floor(bound) if closed else math.ceil(bound) - 1)


def _negate(value: Decimal | None) -> Decimal | None:
    return None if value is None else -value


# --- Builders: the only way adapters should create outcomes ---------------


def moneyline(event: Event, team: TeamId, period: Period = Period.FULL_GAME) -> Outcome:
    """`team` wins. Whether a tie refunds or loses is a Terms concern, not part of the outcome."""
    return _team_margin(event, team, Threshold(Comparator.GT, Decimal(0)), period)


def spread(
    event: Event, team: TeamId, handicap: Decimal, period: Period = Period.FULL_GAME
) -> Outcome:
    """`team` covers `handicap`: -3.5 means win by 4 or more; +3.5 means lose by at most 3."""
    return _team_margin(event, team, Threshold(Comparator.GT, -handicap), period)


def margin_exactly(
    event: Event, team: TeamId, margin: Decimal, period: Period = Period.FULL_GAME
) -> Outcome:
    """`team` wins by exactly `margin` (0 is a tie). Describes pushes on integer lines."""
    return _team_margin(event, team, Threshold(Comparator.EQ, margin), period)


def total(
    event: Event,
    comparator: Comparator,
    line: Decimal,
    team: TeamId | None = None,
    period: Period = Period.FULL_GAME,
) -> Outcome:
    """Game total, or a team total when `team` is given."""
    if team is not None:
        _require_participant(event, team)
    quantity = Quantity(event.league, event.id, Stat.SCORE, period, team_id=team)
    return Outcome(quantity, Threshold(comparator, line))


def player_stat(
    event: Event,
    team: TeamId,
    player: PlayerId,
    stat: Stat,
    comparator: Comparator,
    line: Decimal,
    period: Period = Period.FULL_GAME,
) -> Outcome:
    _require_participant(event, team)
    quantity = Quantity(event.league, event.id, stat, period, team_id=team, player_id=player)
    return Outcome(quantity, Threshold(comparator, line))


def _team_margin(event: Event, team: TeamId, condition: Threshold, period: Period) -> Outcome:
    """Express a condition on `team`'s margin as one on home-minus-away."""
    _require_participant(event, team)
    home_condition = condition if team == event.home else condition.negated()
    return Outcome(Quantity(event.league, event.id, Stat.MARGIN, period), home_condition)


def _require_participant(event: Event, team: TeamId) -> None:
    if not event.involves(team):
        raise ValueError(f"{team} is not playing in {event.id}")
