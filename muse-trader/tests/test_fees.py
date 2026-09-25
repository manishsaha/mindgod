from edge_engine.pricing.fees import DEFAULTS, FeeSchedule, for_venue


def test_kalshi_fee_rounds_up_per_order():
    sched = DEFAULTS["kalshi"]
    assert sched.taker_fee_total(100, 0.5) == 1.75
    assert sched.taker_fee_per_contract(100, 0.5) == 0.0175


def test_fee_peaks_at_50c():
    sched = DEFAULTS["kalshi"]
    assert sched.taker_fee_per_contract(100, 0.5) > sched.taker_fee_per_contract(100, 0.9)
    assert sched.taker_fee_total(100, 0.9) == 0.63


def test_rounding_penalizes_small_orders():
    sched = DEFAULTS["kalshi"]
    # One contract at 50c: raw fee 1.75c rounds up to 2c.
    assert sched.taker_fee_per_contract(1, 0.5) == 0.02
    assert sched.taker_fee_per_contract(1, 0.5) > sched.taker_fee_per_contract(100, 0.5)


def test_for_venue_defaults_and_overrides():
    assert for_venue("kalshi").taker_rate == 0.07
    custom = for_venue("kalshi", {"taker_rate": 0.05, "round_up_cents": False})
    assert custom.taker_rate == 0.05 and custom.round_up_cents is False
    unknown = for_venue("new-venue")
    assert unknown.taker_rate == 0.0


def test_no_round_up_when_disabled():
    sched = FeeSchedule(venue="x", taker_rate=0.07, round_up_cents=False)
    assert abs(sched.taker_fee_total(1, 0.5) - 0.0175) < 1e-9
