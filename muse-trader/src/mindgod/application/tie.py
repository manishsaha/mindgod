"""ADR-0011: the NFL moneyline tie refund is an adjustable terms difference.

A sportsbook's 2-way NFL moneyline refunds the stake on a tie; Kalshi's
"team wins" contract settles No on a tie. Under the equal-terms rule these
never match, so no NFL moneyline listing ever gets a fair value (and no
closing line can form either).

The difference is a single, well-understood quantity: the tie probability
t. The book's devigged price is P(win | no tie); the listing needs the
unconditional P(win):

    P(home wins) = P_book(home) * (1 - t)
    P(away wins) = P_book(away) * (1 - t)

No-side listings derive from the yes basis as 1 - P, which automatically
puts the tie mass on the No side:

    1 - P_book(home)(1 - t) = P_book(away)(1 - t) + t.

The tie rate's own uncertainty (nfl_tie_prob_se) is added to the fair
value's standard error in quadrature, as in ADR-0007.

Like the pitcher rule (ADR-0009), this is a pricing-layer rule keyed on
(league, period, stat, terms difference). Adapters keep reporting terms as
they are; nothing is adjusted at the adapter layer.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal

from mindgod.domain.primitives import Probability
from mindgod.domain.propositions import Comparator, Outcome, Quantity, Threshold
from mindgod.domain.sports import League, Period
from mindgod.domain.stats import Stat
from mindgod.domain.terms import Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing

from .pricing import parse_terms_key

log = logging.getLogger("mindgod.tie")

# ADR-0011: conservative estimate of the modern-overtime NFL tie rate,
# recalibrated each season from results.
NFL_TIE_PROB = 0.004
NFL_TIE_PROB_SE = 0.003

_TIE_LINE = Decimal(0)

# Canonical moneyline shapes: the yes basis is home/away wins, i.e.
# "home margin >= 1" / "home margin <= -1". The No sides are the exact
# complements, "home margin <= 0" / "home margin >= 0".
_MONEYLINE_YES_SIDES = frozenset(
    {
        (Comparator.GTE, Decimal(1)),
        (Comparator.LTE, Decimal(-1)),
    }
)


def _is_nfl_full_game_margin(quantity: Quantity) -> bool:
    return (
        quantity.league is League.NFL
        and quantity.period is Period.FULL_GAME
        and quantity.stat is Stat.MARGIN
    )


def yes_basis(outcome: Outcome) -> Outcome | None:
    """The yes-side moneyline outcome for a Yes or No NFL moneyline outcome.

    Returns the outcome itself when it is already the yes basis ("home
    margin >= 1" / "home margin <= -1"), the complement's yes basis for a
    No-side outcome, and None for anything that is not an NFL full-game
    moneyline side (spreads, totals, other leagues/periods...).
    """
    if not _is_nfl_full_game_margin(outcome.quantity):
        return None
    cond = outcome.condition
    if not isinstance(cond, Threshold):
        return None
    if (cond.comparator, cond.line) in _MONEYLINE_YES_SIDES:
        return outcome
    comp = outcome.complement()
    if comp is None:
        return None
    comp_cond = comp.condition
    if (
        isinstance(comp_cond, Threshold)
        and (comp_cond.comparator, comp_cond.line) in _MONEYLINE_YES_SIDES
    ):
        return comp
    return None


def tie_aware_counterpart(basis: Outcome) -> Outcome | None:
    """The book's other moneyline side under the tie rule.

    For the yes basis "home margin >= 1" the book's away moneyline is "home
    margin <= -1", not the exact complement "home margin <= 0" (which the
    book never quotes: a tie refunds). Only the two moneyline shapes pair;
    anything else fails closed.
    """
    if not _is_nfl_full_game_margin(basis.quantity):
        return None
    cond = basis.condition
    if not isinstance(cond, Threshold):
        return None
    if (cond.comparator, cond.line) not in _MONEYLINE_YES_SIDES:
        return None
    return Outcome(basis.quantity, cond.negated())


def tie_close_basis(listing: Listing) -> Outcome | None:
    """Yes-basis outcome when the ADR-0011 tie rule governs this listing's close.

    Applies to NFL full-game moneyline listings whose terms lose on a tie
    (Kalshi style). Returns None when the rule does not apply.
    """
    if listing.terms.payoff.refunds_if is not None:
        return None
    return yes_basis(listing.outcome)


def find_tie_adjusted_fair(
    fair_by_terms: dict[str, dict[Outcome, FairValue]],
    *,
    yes_outcome: Outcome,
    listing_terms: Terms,
    tie_prob: float = NFL_TIE_PROB,
    tie_prob_se: float = NFL_TIE_PROB_SE,
) -> FairValue | None:
    """ADR-0011: convert a tie-refunding book consensus to listing terms.

    Scans the terms partitions for a book partition on the same event whose
    terms refund on the margin-0 tie with an otherwise identical void
    policy, and converts its yes-basis fair value: P(win) = P_book(win) *
    (1 - t), with the tie-rate uncertainty added to the SE in quadrature.

    Returns None when the rule does not apply (wrong league/period/stat,
    the listing itself refunds ties, not a moneyline shape) or when no
    tie-refunding partition carries the yes outcome.
    """
    if not _is_nfl_full_game_margin(yes_outcome.quantity):
        return None
    if listing_terms.payoff.refunds_if is not None:
        # The listing already refunds ties: the equal-terms rule handles it.
        return None
    if yes_basis(yes_outcome) != yes_outcome:
        return None
    tie_outcome = Outcome(yes_outcome.quantity, Threshold(Comparator.EQ, _TIE_LINE))
    for partition_key, by_outcome in fair_by_terms.items():
        parsed = parse_terms_key(partition_key)
        if parsed is None:
            continue
        refunds, void = parsed
        if refunds != tie_outcome:
            continue
        policy = listing_terms.void_policy
        if (
            void.on_player_absent != policy.on_player_absent
            or void.on_postponement != policy.on_postponement
            or void.listed_pitchers != policy.listed_pitchers
        ):
            continue
        book_fair = by_outcome.get(yes_outcome)
        if book_fair is None:
            continue
        prob = float(book_fair.probability.value) * (1.0 - tie_prob)
        se = math.sqrt(book_fair.standard_error**2 + tie_prob_se**2)
        log.debug(
            "tie adjustment for %s: book %.4f -> listing %.4f (t=%.4f)",
            yes_outcome,
            float(book_fair.probability.value),
            prob,
            tie_prob,
        )
        return FairValue(
            outcome=yes_outcome,
            probability=Probability(prob),
            standard_error=se,
            as_of=book_fair.as_of,
            method=f"{book_fair.method}+tie-adj",
            sources=book_fair.sources,
        )
    return None
