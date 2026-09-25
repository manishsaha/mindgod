"""Fair-value models: devig sportsbook prices into consensus probabilities.

Devig methods are pluggable strategies, chosen empirically per sport and
market type. The pipeline is: devig each book's outcome set on its own, then
partition the devigged prices by settlement terms, then weight books into a
FairValue carrying its standard error and lineage.

Devigging runs per market group *before* terms partitioning on purpose: a
terms bug must never be able to silently switch the vig removal off. A lone
side is dropped, never passed through: one price carries the full overround
and there is nothing to remove it against.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mindgod.domain.primitives import Probability
from mindgod.domain.propositions import Band, CategoryIs, Outcome, Threshold
from mindgod.domain.terms import Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import ListingKey, VenueId

from .ports import PricedOutcome

log = logging.getLogger("mindgod.pricing")


def outcome_key(outcome: Outcome) -> str:
    """Stable storage key for an outcome.

    repr(Outcome) changes whenever the dataclass changes; this serializes the
    canonical structure instead, so stored rows stay joinable across refactors.
    """
    q = outcome.quantity
    parts = [
        q.league.value,
        str(q.event_id),
        q.stat.value,
        q.period.value,
        str(q.team_id) if q.team_id is not None else "",
        str(q.player_id) if q.player_id is not None else "",
    ]
    cond = outcome.condition
    if isinstance(cond, Threshold):
        parts += ["threshold", cond.comparator.value, str(cond.line)]
    elif isinstance(cond, Band):
        parts += ["band", str(cond.lower), str(cond.upper)]
    elif isinstance(cond, CategoryIs):
        parts += ["category", cond.value]
    else:  # pragma: no cover - the domain only has these three conditions
        raise TypeError(f"unknown condition: {type(cond)}")
    return "|".join(parts)


def terms_key(terms: Terms) -> str:
    """What the equal-terms rule compares: refund terms plus void policy.

    The wins_if outcome is the key the fair value is looked up under, so it
    is not part of this key. Two prices are directly comparable exactly when
    their terms keys match, which is differences() restricted to the part
    that is not already established by the outcome lookup.
    """
    refunds = terms.payoff.refunds_if
    void = terms.void_policy
    pitchers = ",".join(sorted(str(p) for p in void.listed_pitchers))
    return "|".join(
        [
            outcome_key(refunds) if refunds is not None else "",
            void.on_player_absent.value,
            void.on_postponement.value,
            pitchers,
        ]
    )


def partition_by_terms(
    priced: list[PricedOutcome],
) -> dict[str, list[PricedOutcome]]:
    """Group priced outcomes by settlement terms.

    Devigging runs per market group inside each partition, so a
    push-refunding whole-number book line never shares a consensus with a
    no-push half-point line on the same canonical outcome.
    """
    groups: dict[str, list[PricedOutcome]] = defaultdict(list)
    for p in priced:
        groups[terms_key(p.terms)].append(p)
    return groups


def devig(probs: list[float], method: str = "power") -> list[float]:
    """Remove the bookmaker margin so probabilities sum to 1.

    Methods: additive (split overround evenly), multiplicative (scale down),
    power (Lopez exponent fit, the standard for two-outcome markets).

    A lone side is refused, not passed through: one price carries the full
    overround, so "devigging" it would bake the vig into the fair value.
    """
    if not probs or any(p <= 0 for p in probs):
        raise ValueError("probabilities must be positive")
    if len(probs) == 1:
        raise ValueError("cannot devig a lone side: one price carries the full overround")
    if method == "additive":
        overround = sum(probs) - 1.0
        return [p - overround / len(probs) for p in probs]
    if method == "multiplicative":
        total = sum(probs)
        return [p / total for p in probs]
    if method == "power":
        lo, hi = 0.01, 50.0
        for _ in range(100):
            mid = (lo + hi) / 2
            if sum(p**mid for p in probs) > 1.0:
                lo = mid
            else:
                hi = mid
        k = (lo + hi) / 2
        return [p**k for p in probs]
    raise ValueError(f"unknown devig method: {method}")


def consensus(fair_by_book: list[tuple[float, float]]) -> float:
    """Weighted average of per-book fair probabilities.

    Each entry is (fair_prob, weight). Weights encode measured predictive
    power, not reputation.
    """
    total_w = sum(w for _, w in fair_by_book)
    if total_w <= 0:
        raise ValueError("weights must sum positive")
    return sum(p * w for p, w in fair_by_book) / total_w


def _weighted_std(entries: list[tuple[float, float]], mean: float) -> float:
    """Disagreement across books: the standard error on the consensus."""
    if len(entries) < 2:
        return 0.0
    total_w = sum(w for _, w in entries)
    if total_w <= 0:
        return 0.0
    var: float = sum(w * (p - mean) ** 2 for p, w in entries) / total_w
    return math.sqrt(var)


@dataclass(frozen=True, slots=True)
class DeviggedPrice:
    """One book's fair probability for an outcome, vig removed.

    Devigging runs per market group before terms partitioning, so both sides
    of a market are always devigged together no matter what their terms look
    like. `age_s` is how stale the quote was when the fair value was built;
    staleness widens the error bar instead of silently passing as fresh.
    """

    outcome: Outcome
    listing_key: ListingKey
    fair_probability: float
    weight: float
    terms: Terms
    age_s: float


def devig_market_groups(
    priced: list[PricedOutcome],
    method: str,
    weight_of: Callable[[VenueId], float],
    as_of: datetime,
    max_quote_age_s: float,
) -> list[DeviggedPrice]:
    """Devig each market group on its own, before terms partitioning.

    Quotes older than `max_quote_age_s` are dropped: a stale quote compared
    against a live exchange book manufactures fake edges. Market groups left
    with a lone side are dropped too: one price carries the full overround.
    """
    groups: dict[str, list[PricedOutcome]] = defaultdict(list)
    for p in priced:
        age = (as_of - p.quote.observed.valid_at).total_seconds()
        if age > max_quote_age_s:
            log.warning(
                "dropping quote older than %.0fs: %s (age %.0fs)",
                max_quote_age_s,
                p.listing_key,
                age,
            )
            continue
        groups[p.market_group].append(p)
    out: list[DeviggedPrice] = []
    for name, group in groups.items():
        if len(group) < 2:
            log.warning(
                "dropping lone side in market group %s: %s carries the full overround",
                name,
                group[0].listing_key,
            )
            continue
        if len({terms_key(p.terms) for p in group}) > 1:
            log.error(
                "market group %s mixes settlement terms; devigging together anyway",
                name,
            )
        implied = [p.quote.implied_probability.value for p in group]
        for p, fair in zip(group, devig(implied, method), strict=True):
            age = max(0.0, (as_of - p.quote.observed.valid_at).total_seconds())
            out.append(
                DeviggedPrice(
                    outcome=p.outcome,
                    listing_key=p.listing_key,
                    fair_probability=fair,
                    weight=weight_of(p.listing_key.venue_id),
                    terms=p.terms,
                    age_s=age,
                )
            )
    return out


class WeightedConsensusModel:
    """The FairValueModel: devig per book-market, weight books, add error bars."""

    def __init__(
        self,
        method: str = "power",
        book_weights: dict[str, float] | None = None,
        default_weight: float = 1.0,
        min_standard_error: float = 0.0,
        max_quote_age_s: float = 900.0,
        stale_se_per_minute: float = 0.001,
    ) -> None:
        self._method = method
        self._book_weights = book_weights or {}
        self._default_weight = default_weight
        # A single book gives a measured disagreement of 0, which drops the
        # uncertainty penalty exactly when uncertainty is highest. The floor
        # keeps one-book fair values honest.
        self._min_standard_error = min_standard_error
        # Quotes older than this never become fair values: comparing a stale
        # consensus against a live book is the stale-quote edge in reverse.
        self._max_quote_age_s = max_quote_age_s
        # ...and quotes approaching the gate carry a wider error bar, so an
        # aging consensus is penalized before it is dropped.
        self._stale_se_per_minute = stale_se_per_minute

    def _weight(self, venue_id: VenueId) -> float:
        return self._book_weights.get(str(venue_id), self._default_weight)

    def values_by_terms(
        self, priced: list[PricedOutcome], as_of: datetime
    ) -> dict[str, dict[Outcome, FairValue]]:
        """Fair values keyed by terms key, then outcome.

        Devigging already ran per market group, so each partition only does
        the cross-book consensus: a push-refunding whole-number line never
        informs a no-push listing's fair value.
        """
        devigged = devig_market_groups(
            priced, self._method, self._weight, as_of, self._max_quote_age_s
        )
        partitions: dict[str, list[DeviggedPrice]] = defaultdict(list)
        for d in devigged:
            partitions[terms_key(d.terms)].append(d)
        result: dict[str, dict[Outcome, FairValue]] = {}
        for key, bucket in partitions.items():
            by_outcome: dict[Outcome, list[DeviggedPrice]] = defaultdict(list)
            for d in bucket:
                by_outcome[d.outcome].append(d)
            fair_values: dict[Outcome, FairValue] = {}
            for outcome, entries in by_outcome.items():
                prob = consensus([(e.fair_probability, e.weight) for e in entries])
                se = max(
                    _weighted_std([(e.fair_probability, e.weight) for e in entries], prob),
                    self._min_standard_error,
                    max(e.age_s for e in entries) / 60.0 * self._stale_se_per_minute,
                )
                fair_values[outcome] = FairValue(
                    outcome=outcome,
                    probability=Probability(prob),
                    standard_error=se,
                    as_of=as_of,
                    method=f"{self._method}-devig",
                    sources=tuple(e.listing_key for e in entries),
                )
            result[key] = fair_values
        return result
