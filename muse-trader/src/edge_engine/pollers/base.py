"""Poller base: every venue adapter implements poll()."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class MarketSnapshot:
    event_key: str            # canonical key once mapped; venue id before
    venue: str                # "kalshi" | "polymarket" | bookmaker name
    market_id: str            # venue-native id (ticker, token id, event id)
    label: str                # human-readable, e.g. "Chiefs vs Bills"
    outcome: str              # "yes" or "no"
    price: float              # price to BUY this outcome, in [0,1]
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict = field(default_factory=dict)


class Poller:
    venue: str = "base"

    async def poll(self) -> list[MarketSnapshot]:
        raise NotImplementedError
