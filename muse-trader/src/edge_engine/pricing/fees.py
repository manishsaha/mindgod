"""Venue fee schedules, loaded from configuration.

The taker fee is price-dependent: it peaks at 50c and shrinks toward the
tails. A flat edge threshold applied to gross edge is therefore wrong; the
gate must be applied to edge net of fees. Because fees round up per order,
small orders pay a much higher effective rate, which the net-edge gate
handles naturally (they fail it).

Fee schedules change over time and differ across markets, so they live in
config, not in code.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    venue: str
    taker_rate: float          # e.g. 0.07 for Kalshi
    round_up_cents: bool = True

    def taker_fee_total(self, contracts: float, price: float) -> float:
        """Total taker fee for an order of `contracts` at `price`."""
        raw = self.taker_rate * contracts * price * (1.0 - price)
        if self.round_up_cents:
            return math.ceil(round(raw, 9) * 100.0) / 100.0
        return raw

    def taker_fee_per_contract(self, contracts: float, price: float) -> float:
        """Effective fee per contract. Explodes for tiny orders when the
        venue rounds up per order; that is the intended behavior."""
        if contracts <= 0:
            return 0.0
        return self.taker_fee_total(contracts, price) / contracts


# Fallbacks used when config provides no fee section for a venue.
DEFAULTS: dict[str, FeeSchedule] = {
    "kalshi": FeeSchedule(venue="kalshi", taker_rate=0.07, round_up_cents=True),
    # Polymarket's taker fee varies by market (~3% sports to ~7% crypto of a
    # price-tied formula). Treat as configurable approximation; verify.
    "polymarket": FeeSchedule(venue="polymarket", taker_rate=0.03,
                              round_up_cents=False),
}


def for_venue(venue: str, overrides: dict | None = None) -> FeeSchedule:
    base = DEFAULTS.get(venue, FeeSchedule(venue=venue, taker_rate=0.0))
    if not overrides:
        return base
    return FeeSchedule(
        venue=venue,
        taker_rate=overrides.get("taker_rate", base.taker_rate),
        round_up_cents=overrides.get("round_up_cents", base.round_up_cents),
    )
