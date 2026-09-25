"""Pricing tests: devig math and the consensus fair-value model."""
from datetime import UTC, datetime

from mindgod.application.ports import PricedOutcome
from mindgod.application.pricing import WeightedConsensusModel, consensus, devig
from mindgod.domain.primitives import Observation
from mindgod.domain.propositions import moneyline
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, EventId, League, TeamId
from mindgod.domain.venues import ListingKey, VenueId

NOW = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
KC, BUF = TeamId("nfl-kc"), TeamId("nfl-buf")
GAME = Event(EventId("nfl-buf-at-kc-20261005"), League.NFL, KC, BUF, NOW)


def _priced(book: str, team: TeamId, odds: int, group: str) -> PricedOutcome:
    key = ListingKey(VenueId(book), "game", f"h2h:{team}")
    return PricedOutcome(
        outcome=moneyline(GAME, team),
        listing_key=key,
        quote=SportsbookQuote(key, odds, Observation(NOW, NOW)),
        market_group=group,
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
