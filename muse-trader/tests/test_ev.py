from edge_engine.engine.ev import evaluate, ev_per_contract, kelly_fraction
from edge_engine.pricing.fees import for_venue

KALSHI = for_venue("kalshi")


def base_kwargs(**over):
    kw = dict(
        event_key="e1",
        venue="kalshi",
        side="yes",
        market_price=0.5,
        fair_prob=0.6,
        bankroll=10000.0,
        min_net_edge=0.02,
        kelly_fraction_mult=0.25,
        max_stake=100.0,
        fee_schedule=KALSHI,
        slippage=0.0,
    )
    kw.update(over)
    return kw


def test_ev_is_fair_minus_price_minus_fee():
    assert abs(ev_per_contract(0.6, 0.5) - 0.1) < 1e-9
    assert abs(ev_per_contract(0.6, 0.5, 0.0175) - 0.0825) < 1e-9


def test_kelly_formula():
    # q=0.6, p=0.5 -> (0.6-0.5)/(1-0.5) = 0.2
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-9
    assert kelly_fraction(0.4, 0.5) == 0.0  # no edge, no bet


def test_evaluate_gates_on_net_edge_and_caps_stake():
    s = evaluate(**base_kwargs())
    assert s is not None
    # gross 0.10 - fee 0.0175 (200 contracts at 50c) = 0.0825 net
    assert abs(s.edge - 0.0825) < 1e-9
    assert abs(s.fee_per_contract - 0.0175) < 1e-9
    # final Kelly on net edge: 0.165 * 0.25 * 10000 = 412.5, capped at 100
    assert s.stake == 100.0
    assert abs(s.ev_per_dollar - 0.0825 / 0.5) < 1e-9


def test_price_dependent_threshold():
    """Same 2% gross edge: rejected at 50c (fee 1.75c), accepted at 90c."""
    kw = base_kwargs(fair_prob=0.52, min_net_edge=0.01)
    assert evaluate(**kw) is None  # 0.02 - 0.0175 = 0.0025 < 0.01
    s = evaluate(**base_kwargs(market_price=0.9, fair_prob=0.92,
                               min_net_edge=0.01))
    assert s is not None  # 0.02 - 0.0063 = 0.0137 >= 0.01


def test_round_up_kills_small_orders():
    """Same market and edge: a tiny order fails the net gate because the
    per-order round-up inflates its effective fee."""
    kw = dict(market_price=0.9, fair_prob=0.92, min_net_edge=0.01)
    assert evaluate(**base_kwargs(bankroll=10.0, **kw)) is None
    assert evaluate(**base_kwargs(bankroll=10000.0, **kw)) is not None


def test_no_signal_below_net_threshold():
    s = evaluate(**base_kwargs(fair_prob=0.51))
    assert s is None  # gross 0.01 < fee alone


def test_no_fee_schedule_falls_back_to_gross_minus_slippage():
    s = evaluate(**base_kwargs(fee_schedule=None, fair_prob=0.53,
                               slippage=0.005))
    assert s is not None
    assert abs(s.edge - (0.03 - 0.005)) < 1e-9


def test_no_side_flips_price_and_fair():
    # Yes priced 0.6 with fair 0.4 -> buying No at 0.4 with fair 0.6: edge.
    s = evaluate(**base_kwargs(side="no", market_price=0.6, fair_prob=0.4))
    assert s is not None
    assert s.side == "no"
    assert abs(s.market_price - 0.4) < 1e-9
    assert abs(s.fair_prob - 0.6) < 1e-9
    # Yes priced 0.5 with fair 0.4 -> No at 0.5, fair 0.6... no wait:
    # Yes 0.5 fair 0.4 -> No costs 0.5, No fair 0.6 -> edge 0.1, signal.
    # Flip it: Yes 0.4 fair 0.5 -> No costs 0.6, No fair 0.5 -> no edge.
    s2 = evaluate(**base_kwargs(side="no", market_price=0.4, fair_prob=0.5))
    assert s2 is None
