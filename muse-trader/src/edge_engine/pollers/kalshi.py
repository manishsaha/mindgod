"""Kalshi poller. Auth: Ed25519 request signing (see docs/api-reference.md).

Endpoints (verify prod host before shipping):
  GET {BASE}/markets
  GET {BASE}/markets/{ticker}
  GET {BASE}/markets/{ticker}/orderbook
"""
from __future__ import annotations

import httpx

from .base import MarketSnapshot, Poller

BASE = "https://external-api.kalshi.com/trade-api/v2"  # verify prod host


class KalshiPoller(Poller):
    venue = "kalshi"

    def __init__(self, key_id: str = "", private_key_path: str = "") -> None:
        self.key_id = key_id
        self.private_key_path = private_key_path

    def _headers(self, method: str, path: str) -> dict:
        # TODO: implement Ed25519 signing -> KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE
        return {}

    async def poll(self) -> list[MarketSnapshot]:
        # TODO: GET /markets with pagination; map tickers to MarketSnapshots.
        # Market data endpoints are public (no auth needed for quotes).
        async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
            resp = await client.get("/markets", params={"limit": 200})
            resp.raise_for_status()
            data = resp.json()
        snapshots: list[MarketSnapshot] = []
        for m in data.get("markets", []):
            ticker = m.get("ticker", "")
            yes_price = (m.get("yes_ask") or 0) / 100.0
            snapshots.append(
                MarketSnapshot(
                    event_key=ticker,
                    venue=self.venue,
                    market_id=ticker,
                    label=m.get("title", ticker),
                    outcome="yes",
                    price=yes_price,
                    bid=(m.get("yes_bid") or 0) / 100.0,
                    ask=yes_price,
                    volume=m.get("volume"),
                    raw=m,
                )
            )
        return snapshots
