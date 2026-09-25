"""Settlement terms: how an outcome turns into money at a particular venue.

Two listings are the same bet only when their Terms match. Most "arbitrages"
are two different bets, e.g. an NFL moneyline that refunds on a tie versus a
contract that settles NO on a tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mindgod.domain.propositions import Outcome
from mindgod.domain.sports import PlayerId


class AbsenceRule(StrEnum):
    VOID = "void"  # stake returned if the player does not participate
    ACTION = "action"  # settles on recorded stats regardless


class PostponementRule(StrEnum):
    VOID = "void"
    ACTION_IF_PLAYED_WITHIN_WINDOW = "action_if_played_within_window"


@dataclass(frozen=True, slots=True)
class Payoff:
    """Win if `wins_if` happens, refund if `refunds_if` happens, otherwise lose."""

    wins_if: Outcome
    refunds_if: Outcome | None = None


@dataclass(frozen=True, slots=True)
class VoidPolicy:
    on_player_absent: AbsenceRule = AbsenceRule.VOID
    on_postponement: PostponementRule = PostponementRule.VOID
    listed_pitchers: frozenset[PlayerId] = frozenset()  # MLB books: void unless these start


@dataclass(frozen=True, slots=True)
class Terms:
    payoff: Payoff
    void_policy: VoidPolicy = VoidPolicy()


class TermsDifference(StrEnum):
    OUTCOME = "outcome"
    REFUND = "refund"
    PLAYER_ABSENT = "player_absent"
    POSTPONEMENT = "postponement"
    LISTED_PITCHERS = "listed_pitchers"


def differences(a: Terms, b: Terms) -> frozenset[TermsDifference]:
    """How two venues' terms differ. Empty means prices are directly comparable."""
    checks = {
        TermsDifference.OUTCOME: a.payoff.wins_if != b.payoff.wins_if,
        TermsDifference.REFUND: a.payoff.refunds_if != b.payoff.refunds_if,
        TermsDifference.PLAYER_ABSENT: (
            a.void_policy.on_player_absent != b.void_policy.on_player_absent
        ),
        TermsDifference.POSTPONEMENT: (
            a.void_policy.on_postponement != b.void_policy.on_postponement
        ),
        TermsDifference.LISTED_PITCHERS: (
            a.void_policy.listed_pitchers != b.void_policy.listed_pitchers
        ),
    }
    return frozenset(difference for difference, differs in checks.items() if differs)
