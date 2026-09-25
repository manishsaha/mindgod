"""Fair-value models: devig sportsbook prices into consensus probabilities.

Devig methods are pluggable strategies, chosen empirically per sport and
market type. Each book's outcome set is devigged on its own (a book's h2h
pair sums to 1 + vig; a lone side carries no overround to remove and passes
through), then books combine by configured weight into a FairValue carrying
its standard error and lineage.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from mindgod.domain.primitives import Probability
from mindgod.domain.propositions import Band, CategoryIs, Outcome, Threshold
from mindgod.domain.terms import Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import ListingKey, VenueId

from .ports import PricedOutcome


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
    """
    if not probs or any(p <= 0 for p in probs):
        raise ValueError("probabilities must be positive")
    if len(probs) == 1:
        return list(probs)
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


class WeightedConsensusModel:
    """The FairValueModel: devig per book-market, weight books, add error bars."""

    def __init__(
        self,
        method: str = "power",
        book_weights: dict[str, float] | None = None,
        default_weight: float = 1.0,
        min_standard_error: float = 0.0,
    ) -> None:
        self._method = method
        self._book_weights = book_weights or {}
        self._default_weight = default_weight
        # A single book gives a measured disagreement of 0, which drops the
        # uncertainty penalty exactly when uncertainty is highest. The floor
        # keeps one-book fair values honest.
        self._min_standard_error = min_standard_error

    def _weight(self, venue_id: VenueId) -> float:
        return self._book_weights.get(str(venue_id), self._default_weight)

    def value(self, priced: list[PricedOutcome], as_of: datetime) -> dict[Outcome, FairValue]:
        groups: dict[str, list[PricedOutcome]] = defaultdict(list)
        for p in priced:
            groups[p.market_group].append(p)
        by_outcome: dict[Outcome, list[tuple[float, float, ListingKey]]] = defaultdict(list)
        for group in groups.values():
            implied = [p.quote.implied_probability.value for p in group]
            for p, fair in zip(group, devig(implied, self._method), strict=True):
                by_outcome[p.outcome].append(
                    (fair, self._weight(p.listing_key.venue_id), p.listing_key)
                )
        result: dict[Outcome, FairValue] = {}
        for outcome, entries in by_outcome.items():
            prob = consensus([(f, w) for f, w, _ in entries])
            se = max(
                _weighted_std([(f, w) for f, w, _ in entries], prob),
                self._min_standard_error,
            )
            result[outcome] = FairValue(
                outcome=outcome,
                probability=Probability(prob),
                standard_error=se,
                as_of=as_of,
                method=f"{self._method}-devig",
                sources=tuple(key for _, _, key in entries),
            )
        return result
