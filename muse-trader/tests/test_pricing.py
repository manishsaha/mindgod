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


def test_devig_rejects_single_side():
    import pytest

    with pytest.raises(ValueError):
        devig([0.6], "power")


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
    values = model.values_by_terms(priced, NOW)
    assert len(values) == 1
    values = next(iter(values.values()))
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
    values = model.values_by_terms(priced, NOW)
    assert len(values) == 1
    values = next(iter(values.values()))
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
    values = model.values_by_terms(priced, NOW)
    assert len(values) == 1
    values = next(iter(values.values()))
    kc = moneyline(GAME, KC)
    assert values[kc].standard_error == 0.02


def test_outcome_key_is_stable_and_unique():
    kc = moneyline(GAME, KC)
    buf = moneyline(GAME, BUF)
    assert outcome_key(kc) == outcome_key(moneyline(GAME, KC))
    assert outcome_key(kc) != outcome_key(buf)
    assert outcome_key(kc) != outcome_key(spread(GAME, KC, Decimal("-3.5")))


def _spread_priced(book: str, team: TeamId, handicap: str, odds: int) -> PricedOutcome:
    from mindgod.domain.propositions import margin_exactly

    key = ListingKey(VenueId(book), "game", f"spreads:{team}:{handicap}")
    point = Decimal(handicap)
    outcome = spread(GAME, team, point)
    refunds = margin_exactly(GAME, team, -point) if point == point.to_integral_value() else None
    return PricedOutcome(
        outcome=outcome,
        listing_key=key,
        quote=SportsbookQuote(key, odds, Observation(NOW, NOW)),
        market_group=f"{book}:game:spreads",
        terms=Terms(Payoff(outcome, refunds)),
    )


def test_partition_by_terms_separates_push_lines():
    priced = [
        _spread_priced("draftkings", KC, "-3", -110),
        _spread_priced("draftkings", BUF, "3", -110),
        _spread_priced("fanduel", KC, "-3.5", -110),
        _spread_priced("fanduel", BUF, "3.5", -110),
    ]
    partitions = partition_by_terms(priced)
    assert len(partitions) == 2
    whole = [p for p in priced if p.terms.payoff.refunds_if is not None]
    half = [p for p in priced if p.terms.payoff.refunds_if is None]
    assert partitions[terms_key(whole[0].terms)] == whole
    assert partitions[terms_key(half[0].terms)] == half


def test_underdog_push_terms_match_favorite():
    """The -point regression: BUF +3 pushes on the same game as KC -3.

    With abs(point) the underdog's refund terms described a different game
    (BUF winning by 3), splitting the pair into lone-side partitions that
    passed the vig straight through: 52.38% + 52.38% = 104.8%.
    """
    from mindgod.domain.propositions import margin_exactly

    priced = [
        _spread_priced("pinnacle", KC, "-3", -110),
        _spread_priced("pinnacle", BUF, "3", -110),
    ]
    assert terms_key(priced[0].terms) == terms_key(priced[1].terms)
    push = margin_exactly(GAME, KC, Decimal("3"))
    assert priced[0].terms.payoff.refunds_if == push
    assert priced[1].terms.payoff.refunds_if == push
    model = WeightedConsensusModel()
    values = model.values_by_terms(priced, NOW)
    assert len(values) == 1
    fair = next(iter(values.values()))
    kc = spread(GAME, KC, Decimal("-3"))
    buf = spread(GAME, BUF, Decimal("3"))
    assert abs(fair[kc].probability.value - 0.5) < 1e-6
    assert abs(fair[buf].probability.value - 0.5) < 1e-6
    assert abs(fair[kc].probability.value + fair[buf].probability.value - 1.0) < 1e-6


def test_lone_side_is_dropped_not_passed_through():
    priced = [_priced("draftkings", KC, -110, "draftkings:game:h2h")]
    model = WeightedConsensusModel()
    assert model.values_by_terms(priced, NOW) == {}


def test_stale_quotes_are_dropped():
    from datetime import timedelta

    old = NOW - timedelta(minutes=20)
    key = ListingKey(VenueId("draftkings"), "game", "h2h:nfl-kc")
    outcome = moneyline(GAME, KC)
    stale = PricedOutcome(
        outcome=outcome,
        listing_key=key,
        quote=SportsbookQuote(key, -110, Observation(old, NOW)),
        market_group="draftkings:game:h2h",
        terms=Terms(Payoff(outcome)),
    )
    model = WeightedConsensusModel(max_quote_age_s=900)
    assert model.values_by_terms([stale], NOW) == {}


def test_quote_age_widens_standard_error():
    from datetime import timedelta

    aged_at = NOW - timedelta(minutes=10)
    priced = []
    for book in ("draftkings", "fanduel"):
        key = ListingKey(VenueId(book), "game", f"h2h:{book}")
        for team in (KC, BUF):
            outcome = moneyline(GAME, team)
            valid = aged_at if book == "fanduel" else NOW
            priced.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=key,
                    quote=SportsbookQuote(key, -110, Observation(valid, NOW)),
                    market_group=f"{book}:game:h2h",
                    terms=Terms(Payoff(outcome)),
                )
            )
    model = WeightedConsensusModel(stale_se_per_minute=0.001)
    values = model.values_by_terms(priced, NOW)
    fair = next(iter(values.values()))
    kc = moneyline(GAME, KC)
    # 10 minutes stale at 0.001/min beats the zero disagreement floor.
    assert fair[kc].standard_error == 0.01
