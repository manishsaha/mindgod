"""Opportunity tests: the net-edge gate and fractional-Kelly sizing.

Each test protects the fee-aware gating behavior: the threshold applies to
net edge because the taker fee is price-dependent (peaks at 50c, shrinks to
the tails), and per-order round-up punishes tiny orders.
"""

from datetime import UTC, datetime
from decimal import Decimal as D

from mindgod.application.opportunities import (
    DetectorConfig,
    ValueDetector,
    evaluate,
)
from mindgod.domain.fees import QuadraticFeeModel
from mindgod.domain.primitives import ContractPrice, Observation, Probability
from mindgod.domain.propositions import moneyline
from mindgod.domain.quotes import OrderBook, PriceLevel
from mindgod.domain.sports import Event, EventId, League, TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing, ListingKey, VenueId

NOW = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
KC, BUF = TeamId("nfl-kc"), TeamId("nfl-buf")
GAME = Event(EventId("nfl-buf-at-kc-20261005"), League.NFL, KC, BUF, NOW)
KEY = ListingKey(VenueId("kalshi"), "T1", "yes")
FEES = QuadraticFeeModel(D("0.07"), D("0"))


def _listing(outcome=None, side="yes") -> Listing:
    return Listing(
        ListingKey(VenueId("kalshi"), "T1", side),
        Terms(Payoff(outcome if outcome is not None else moneyline(GAME, KC))),
    )


def _book(price: str, size: int = 10000, key: ListingKey = KEY) -> OrderBook:
    return OrderBook(
        key,
        asks=(PriceLevel(ContractPrice(D(price)), size),),
        bids=(),
        observed=Observation(NOW, NOW),
    )


def _fair(prob: float, se: float = 0.0, outcome=None) -> FairValue:
    return FairValue(
        outcome if outcome is not None else moneyline(GAME, KC),
        Probability(prob),
        se,
        NOW,
        "test",
    )


def _cfg(**over) -> DetectorConfig:
    kw: dict = dict(
        bankroll=D(10000),
        min_net_edge=D("0.02"),
        kelly_fraction=D("0.25"),
        max_stake_per_bet=D("100"),
        slippage=D("0"),
    )
    kw.update(over)
    return DetectorConfig(**kw)


def test_gates_on_net_edge_and_caps_stake():
    opp = evaluate(_listing(), _book("0.5"), _fair(0.6), FEES, _cfg())
    assert opp is not None
    # gross 0.10 - fee 0.0175 (200 contracts at 50c) = 0.0825 net
    assert abs(float(opp.edge_net) - 0.0825) < 1e-9
    assert abs(float(opp.fee) - 3.50) < 1e-9  # total fee for the 200-contract order
    # final Kelly on net edge: 0.165 * 0.25 * 10000 = 412.5, capped at 100
    assert opp.stake == D("100")


def test_price_dependent_threshold():
    """Same 2% gross edge: rejected at 50c (fee 1.75c), accepted at 90c."""
    cfg = _cfg(min_net_edge=D("0.01"))
    assert evaluate(_listing(), _book("0.5"), _fair(0.52), FEES, cfg) is None
    opp = evaluate(_listing(), _book("0.9"), _fair(0.92), FEES, cfg)
    assert opp is not None  # 0.02 - 0.0063 = 0.0137 >= 0.01


def test_round_up_kills_small_orders():
    """Same market and edge: a tiny order fails the net gate because the
    per-order round-up inflates its effective fee."""
    kw: dict = dict(min_net_edge=D("0.01"))
    assert evaluate(_listing(), _book("0.9"), _fair(0.92), FEES, _cfg(bankroll=D(10), **kw)) is None
    assert evaluate(_listing(), _book("0.9"), _fair(0.92), FEES, _cfg(**kw)) is not None


def test_no_signal_below_net_threshold():
    assert evaluate(_listing(), _book("0.5"), _fair(0.51), FEES, _cfg()) is None


def test_no_asks_no_opportunity():
    book = OrderBook(KEY, asks=(), bids=(), observed=Observation(NOW, NOW))
    assert evaluate(_listing(), book, _fair(0.6), FEES, _cfg()) is None


def test_depth_caps_size():
    opp = evaluate(_listing(), _book("0.5", size=5), _fair(0.6), FEES, _cfg())
    assert opp is not None
    assert opp.fill.contracts == 5
    assert opp.stake == D("2.5")


def test_uncertainty_widens_the_gate():
    cfg = _cfg()
    assert evaluate(_listing(), _book("0.5"), _fair(0.6), FEES, cfg) is not None
    # se 0.05 widens the 0.02 gate to 0.12; the 0.0825 net edge no longer clears
    assert evaluate(_listing(), _book("0.5"), _fair(0.6, se=0.05), FEES, cfg) is None


def test_uncertainty_shrinks_the_stake():
    cfg = _cfg(max_stake_per_bet=D("100000"))
    plain = evaluate(_listing(), _book("0.5"), _fair(0.6), FEES, cfg)
    unsure = evaluate(_listing(), _book("0.5"), _fair(0.6, se=0.02), FEES, cfg)
    assert plain is not None and unsure is not None
    assert unsure.stake < plain.stake


def test_no_side_uses_the_complement():
    """Buying No at 35c with fair P(yes) = 0.6 is buying the complement
    (margin <= 0, tie included) at fair 0.4: a real edge."""
    outcome = moneyline(GAME, KC).complement()
    assert outcome is not None
    listing = _listing(outcome=outcome, side="no")
    key = ListingKey(VenueId("kalshi"), "T1", "no")
    detector = ValueDetector({VenueId("kalshi"): FEES}, _cfg(min_net_edge=D("0.01")))
    opps = detector.detect(
        [(listing, _book("0.35", key=key))],
        {moneyline(GAME, KC): _fair(0.6)},
    )
    assert len(opps) == 1
    assert opps[0].listing.side == "no"


def test_detector_skips_without_fair_value():
    detector = ValueDetector({VenueId("kalshi"): FEES}, _cfg())
    assert detector.detect([(_listing(), _book("0.5"))], {}) == []
