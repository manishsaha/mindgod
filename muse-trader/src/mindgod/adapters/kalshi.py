"""Kalshi: public REST for quotes.

Market-data endpoints need no auth. Order placement needs Ed25519 request
signing and is not wired: the live execution venue fails closed.

Endpoints (verify the prod host before shipping):
  GET {BASE}/markets
  GET {BASE}/markets/{ticker}/orderbook   -> {"orderbook": {"yes": [[c, n], ...]}}
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from mindgod.application.ports import ExchangeSource, UnmappedMarket
from mindgod.domain.primitives import ContractPrice, Observation
from mindgod.domain.quotes import OrderBook, PriceLevel
from mindgod.domain.venues import Listing, ListingKey, VenueId

log = logging.getLogger("mindgod.adapters.kalshi")

BASE = "https://api.elections.kalshi.com/trade-api/v2"  # verify prod host


def _levels(
    raw: list[Any], invert: bool
) -> tuple[PriceLevel, ...]:
    levels: list[PriceLevel] = []
    for cents, contracts in raw:
        price = Decimal(cents)
        if invert:
            price = Decimal(100) - price
        price = price / Decimal(100)
        if not Decimal(0) < price < Decimal(1):
            continue
        size = int(contracts)
        if size <= 0:
            continue
        levels.append(PriceLevel(ContractPrice(price), size))
    return tuple(levels)


def _to_book(key: ListingKey, data: dict[str, Any], now: datetime) -> OrderBook:
    book = data.get("orderbook", {})
    yes = _levels(book.get("yes", []), invert=False)
    no = _levels(book.get("no", []), invert=False)
    if key.side == "no":
        asks = tuple(sorted(no, key=lambda lvl: lvl.price.dollars))
        bids = tuple(
            sorted(
                (
                    PriceLevel(
                        ContractPrice(Decimal(1) - bid_lvl.price.dollars), bid_lvl.contracts
                    )
                    for bid_lvl in yes
                ),
                key=lambda lvl: lvl.price.dollars,
                reverse=True,
            )
        )
    else:
        asks = tuple(sorted(yes, key=lambda lvl: lvl.price.dollars))
        bids = tuple(
            sorted(
                (
                    PriceLevel(
                        ContractPrice(Decimal(1) - bid_lvl.price.dollars), bid_lvl.contracts
                    )
                    for bid_lvl in no
                ),
                key=lambda lvl: lvl.price.dollars,
                reverse=True,
            )
        )
    return OrderBook(
        listing=key, asks=asks, bids=bids, observed=Observation(now, now)
    )


class KalshiExchange(ExchangeSource):
    venue_id = VenueId("kalshi")

    async def order_books(self, listings: list[Listing]) -> list[OrderBook]:
        books: list[OrderBook] = []
        async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
            for listing in listings:
                now = datetime.now(UTC)
                try:
                    resp = await client.get(
                        f"/markets/{listing.key.market_id}/orderbook"
                    )
                    resp.raise_for_status()
                    books.append(
                        _to_book(listing.key, resp.json(), now)
                    )
                except Exception:
                    log.exception(
                        "kalshi orderbook failed for %s", listing.key.market_id
                    )
        return books

    async def discover(self) -> list[UnmappedMarket]:
        now = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
                resp = await client.get("/markets", params={"limit": 200})
                resp.raise_for_status()
                markets = resp.json().get("markets", [])
        except Exception:
            log.exception("kalshi discovery failed")
            return []
        return [
            UnmappedMarket(
                venue_id=self.venue_id,
                market_id=str(m.get("ticker", "")),
                label=str(m.get("title", "")),
                seen_at=now,
                reason="unregistered ticker",
            )
            for m in markets
            if m.get("ticker")
        ]
