"""Pricing tests: devig math and the consensus fair-value model."""

from datetime import UTC, datetime
from decimal import Decimal

from mindgod.application.ports import PricedOutcome
from mindgod.application.pricing import (
    WeightedConsensusModel,
    consensus,
    devig,
    outcome_key,
    partition_by_terms,
    terms_key,
)
from mindgod.domain.primitives import Observation
from mindgod.domain.propositions import moneyline, spread
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, EventId, League, TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import ListingKey, VenueId

NOW = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
KC, BUF = TeamId("nfl-kc"), TeamId("nfl-buf")
GAME = Event(EventId("nfl-buf-at-kc-20261005"), League.NFL, KC, BUF, NOW)


def _priced(book: str, team: TeamId, odds: int, group: str) -> PricedOutcome:
    key = ListingKey(VenueId(book), "game", f"h2h:{team}")
    outcome = moneyline(GAME, team)
    return PricedOutcome(
        outcome=outcome,
        listing_key=key,
        quote=SportsbookQuote(key, odds, Observation(NOW, NOW)),
        market_group=group,
        terms=Terms(Payoff(outcome)),
    )


def test_devig_sums_to_one():
    from mindgod.domain.primitives import Probability

    probs = [
        Probability.from_american_odds(-110).value,
        Probability.from_american_odds(-110).value,
    ]
    for method in ("additive", "multiplicative", "power"):
        fair = devig(probs, method)
        assert abs(sum(fair) - 1.0) < 1e-6
        assert all(0 < p < 1 for p in fair)


def test_devig_removes_margin():
    from mindgod.domain.primitives import Probability

    probs = [
        Probability.from_american_odds(-150).value,
        Probability.from_american_odds(130).value,
    ]
    fair = devig(probs, "power")
    assert abs(sum(fair) - 1.0) < 1e-6
    assert fair[0] > 0.5  # favorite stays favorite


def test_devig_single_side_passes_through():
    assert devig([0.6], "power") == [0.6]


def test_consensus_weighted():
    assert abs(consensus([(0.6, 3.0), (0.5, 1.0)]) - 0.575) < 1e-9


def test_model_builds_fair_value_with_lineage():
    priced = [
        _priced("draftkings", KC, -110, "draftkings:game:h2h"),
        _priced("draftkings", BUF, -110, "draftkings:game:h2h"),
        _priced("fanduel", KC, -110, "fanduel:game:h2h"),
        _priced("fanduel", BUF, -110, "fanduel:game:h2h"),
    ]
    model = WeightedConsensusModel()
    values = model.value(priced, NOW)
    kc = moneyline(GAME, KC)
    assert abs(values[kc].probability.value - 0.5) < 1e-6
    assert values[kc].standard_error == 0.0
    assert values[kc].method == "power-devig"
    assert len(values[kc].sources) == 2


def test_model_weights_sharp_books_and_measures_disagreement():
    priced = [
        _priced("pinnacle", KC, -110, "pinnacle:game:h2h"),
        _priced("pinnacle", BUF, -110, "pinnacle:game:h2h"),
        _priced("draftkings", KC, -150, "draftkings:game:h2h"),
        _priced("draftkings", BUF, 130, "draftkings:game:h2h"),
    ]
    model = WeightedConsensusModel(book_weights={"pinnacle": 3.0})
    values = model.value(priced, NOW)
    kc = moneyline(GAME, KC)
    fair = values[kc].probability.value
    assert 0.5 < fair < 0.6  # sharp book pulls consensus toward 0.5
    assert values[kc].standard_error > 0.0  # books disagree


def test_min_standard_error_floors_single_book_certainty():
    priced = [
        _priced("draftkings", KC, -110, "draftkings:game:h2h"),
        _priced("draftkings", BUF, -110, "draftkings:game:h2h"),
    ]
    model = WeightedConsensusModel(min_standard_error=0.02)
    values = model.value(priced, NOW)
    kc = moneyline(GAME, KC)
    assert values[kc].standard_error == 0.02


def test_outcome_key_is_stable_and_unique():
    kc = moneyline(GAME, KC)
    buf = moneyline(GAME, BUF)
    assert outcome_key(kc) == outcome_key(moneyline(GAME, KC))
    assert outcome_key(kc) != outcome_key(buf)
    assert outcome_key(kc) != outcome_key(spread(GAME, KC, Decimal("-3.5")))


def _spread_priced(book: str, handicap: str, odds: int) -> PricedOutcome:
    from mindgod.domain.propositions import margin_exactly

    key = ListingKey(VenueId(book), "game", f"spreads:{handicap}")
    point = Decimal(handicap)
    outcome = spread(GAME, KC, point)
    refunds = margin_exactly(GAME, KC, abs(point)) if point == point.to_integral_value() else None
    return PricedOutcome(
        outcome=outcome,
        listing_key=key,
        quote=SportsbookQuote(key, odds, Observation(NOW, NOW)),
        market_group=f"{book}:game:spreads",
        terms=Terms(Payoff(outcome, refunds)),
    )


def test_partition_by_terms_separates_push_lines():
    priced = [
        _spread_priced("draftkings", "-3", -110),
        _spread_priced("draftkings", "3", -110),
        _spread_priced("fanduel", "-3.5", -110),
        _spread_priced("fanduel", "3.5", -110),
    ]
    partitions = partition_by_terms(priced)
    assert len(partitions) == 2
    whole = [p for p in priced if p.terms.payoff.refunds_if is not None]
    half = [p for p in priced if p.terms.payoff.refunds_if is None]
    assert partitions[terms_key(whole[0].terms)] == whole
    assert partitions[terms_key(half[0].terms)] == half
