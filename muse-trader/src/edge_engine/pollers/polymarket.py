"""Polymarket pollers: Gamma for discovery, CLOB for orderbook/prices.

Gamma: https://gamma-api.polymarket.com (free, no auth)
CLOB:  https://clob.polymarket.com (public reads; trading needs EIP-712 + HMAC)
"""
from __future__ import annotations

import httpx

from .base import MarketSnapshot, Poller

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


class PolymarketGammaPoller(Poller):
    venue = "polymarket"

    async def poll(self) -> list[MarketSnapshot]:
        async with httpx.AsyncClient(base_url=GAMMA, timeout=15) as client:
            resp = await client.get(
                "/events",
                params={"active": "true", "closed": "false", "limit": 100,
                        "order": "volume24hr", "ascending": "false"},
            )
            resp.raise_for_status()
            data = resp.json()
        snapshots: list[MarketSnapshot] = []
        for event in data if isinstance(data, list) else data.get("events", []):
            for market in event.get("markets", []):
                token_ids = market.get("clobTokenIds")
                if not token_ids:
                    continue
                try:
                    last = float(market.get("lastTradePrice") or 0)
                except (TypeError, ValueError):
                    continue
                snapshots.append(
                    MarketSnapshot(
                        event_key=market.get("conditionId", market.get("id", "")),
                        venue=self.venue,
                        market_id=token_ids[0],
                        label=market.get("question", ""),
                        outcome="yes",
                        price=last,
                        volume=market.get("volumeNum"),
                        raw={"slug": event.get("slug"),
                             "clobTokenIds": token_ids},
                    )
                )
        return snapshots


class PolymarketClobPoller(Poller):
    venue = "polymarket"

    async def book(self, token_id: str) -> dict:
        async with httpx.AsyncClient(base_url=CLOB, timeout=15) as client:
            resp = await client.get("/book", params={"token_id": token_id})
            resp.raise_for_status()
            return resp.json()

    async def poll(self) -> list[MarketSnapshot]:
        # Orderbook polling is driven per token id from Gamma discovery;
        # the scheduler pairs these two pollers.
        return []
