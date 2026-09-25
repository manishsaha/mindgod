from edge_engine.pricing.devig import (
    american_to_prob,
    consensus,
    decimal_to_prob,
    devig,
    prob_to_american,
)


def test_american_conversion_roundtrip():
    # probability-level roundtrip (exact for all p)
    for p in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95):
        assert abs(american_to_prob(prob_to_american(p)) - p) < 1e-9
    # odds-level roundtrip, away from the +/-100 boundary (both imply p=0.5)
    for odds in (-10000, -500, -110, 150, 250, 1000):
        p = american_to_prob(odds)
        assert 0 < p < 1
        assert abs(prob_to_american(p) - odds) < 1e-6


def test_devig_sums_to_one():
    probs = [american_to_prob(-110), american_to_prob(-110)]
    for method in ("additive", "multiplicative", "power"):
        fair = devig(probs, method)
        assert abs(sum(fair) - 1.0) < 1e-6
        assert all(0 < p < 1 for p in fair)


def test_devig_removes_margin():
    probs = [american_to_prob(-150), american_to_prob(130)]
    fair = devig(probs, "power")
    assert abs(sum(fair) - 1.0) < 1e-6
    assert fair[0] > 0.5  # favorite stays favorite


def test_consensus_weighted():
    c = consensus([(0.6, 3.0), (0.5, 1.0)])
    assert abs(c - 0.575) < 1e-9
