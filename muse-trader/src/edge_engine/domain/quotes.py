"""Quotes: bitemporal market observations and order-book depth.

A Quote is append-only and never updated in place. valid_at is when the price
was valid at the venue; observed_at is when we saw it. The gap between them
is the latency the stale-quote detectors trade against. MarketBook carries
the ladder so sizing can later walk it instead of trusting top of book.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..pollers.base import MarketSnapshot

QUOTE_SIDES = ("bid", "ask", "last")


@dataclass(frozen=True)
class Quote:
    venue: str
    venue_market_id: str
    outcome_id: str
    side: str                        # bid | ask | last
    price: float                     # in [0,1]
    canonical_market_id: str | None = None   # None until mapped
    size: float | None = None        # contracts available at this price
    valid_at: datetime | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass
class MarketBook:
    """Depth snapshot for one outcome on one venue."""

    venue: str
    venue_market_id: str
    outcome_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    canonical_market_id: str | None = None
    valid_at: datetime | None = None
    observed_at: datetime | None = None

    def avg_fill_price(self, contracts: float, side: str = "ask") -> float | None:
        """Volume-weighted average price to buy (ask) or sell (bid)
        `contracts`, walking the ladder. Returns None when depth is
        insufficient, which is itself a signal to size down or skip."""
        levels = self.asks if side == "ask" else self.bids
        remaining = contracts
        cost = 0.0
        for lvl in levels:
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return None
        return cost / contracts


def snapshot_to_quote(
    snap: "MarketSnapshot",
    canonical_market_id: str | None,
    observed_at: datetime,
) -> Quote:
    """Convert a poller MarketSnapshot into a domain Quote.

    Pollers report the best ask as the buy price, so snapshots land as
    side="ask". valid_at falls back to the poll timestamp; prefer the
    venue's own server timestamp when the API provides one.
    """
    return Quote(
        venue=snap.venue,
        venue_market_id=snap.market_id,
        outcome_id=snap.outcome,
        side="ask",
        price=snap.price,
        canonical_market_id=canonical_market_id,
        size=None,
        valid_at=snap.ts,
        observed_at=observed_at,
    )
