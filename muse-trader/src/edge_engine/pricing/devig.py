"""Odds conversion and de-vigging.

Turns raw bookmaker quotes into fair (no-vig) probabilities.
"""
from __future__ import annotations


def american_to_prob(odds: float) -> float:
    """Implied probability of American odds."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def decimal_to_prob(odds: float) -> float:
    return 1.0 / odds


def prob_to_american(p: float) -> float:
    if p <= 0 or p >= 1:
        raise ValueError("probability must be in (0, 1)")
    if p >= 0.5:
        return -100.0 * p / (1.0 - p)
    return 100.0 * (1.0 - p) / p


def devig(probs: list[float], method: str = "power") -> list[float]:
    """Remove the bookmaker margin so probabilities sum to 1.

    Methods: additive (split overround evenly), multiplicative (scale down),
    power (Lopez exponent fit, the standard for two-outcome markets).
    """
    if not probs or any(p <= 0 for p in probs):
        raise ValueError("probabilities must be positive")
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

    Each entry is (fair_prob, weight). Returns the consensus fair probability.
    """
    total_w = sum(w for _, w in fair_by_book)
    if total_w <= 0:
        raise ValueError("weights must sum positive")
    return sum(p * w for p, w in fair_by_book) / total_w
