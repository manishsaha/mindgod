"""Polymarket: Gamma for discovery, CLOB for books.

Gamma markets list condition/token ids; the CLOB /book endpoint returns the
ladder per token id. Listings need a token_id in their spec for depth.

US note: Polymarket Global blocks opening positions from US IPs and
Polymarket US is a separate interface. Execution eligibility, fees, and API
access need fresh verification before any live order. The live execution
venue fails closed until then.
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

log = logging.getLogger("mindgod.adapters.polymarket")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _levels(raw: list[Any]) -> tuple[PriceLevel, ...]:
    levels: list[PriceLevel] = []
    for entry in raw:
        try:
            price = Decimal(str(entry["price"]))
            size = int(Decimal(str(entry["size"])))
        except (KeyError, TypeError, ValueError):
            continue
        if not Decimal(0) < price < Decimal(1) or size <= 0:
            continue
        levels.append(PriceLevel(ContractPrice(price), size))
    return tuple(levels)


def _empty_book(key: ListingKey, now: datetime) -> OrderBook:
    """No book, no trade: an empty book fails safe downstream."""
    return OrderBook(listing=key, asks=(), bids=(), observed=Observation(now, now))


def _to_book(key: ListingKey, data: dict[str, Any], now: datetime) -> OrderBook:
    asks = tuple(sorted(_levels(data.get("asks", [])), key=lambda lvl: lvl.price.dollars))
    bids = tuple(
        sorted(_levels(data.get("bids", [])), key=lambda lvl: lvl.price.dollars, reverse=True)
    )
    return OrderBook(listing=key, asks=asks, bids=bids, observed=Observation(now, now))


class PolymarketExchange(ExchangeSource):
    venue_id = VenueId("polymarket")

    def __init__(self, token_ids: dict[str, str] | None = None) -> None:
        # market_id -> CLOB token id, from listing specs
        self._token_ids = token_ids or {}

    async def order_books(self, listings: list[Listing]) -> list[OrderBook]:
        books: list[OrderBook] = []
        async with httpx.AsyncClient(base_url=CLOB, timeout=15) as client:
            for listing in listings:
                token_id = self._token_ids.get(listing.key.market_id)
                now = datetime.now(UTC)
                if not token_id:
                    log.warning(
                        "no CLOB token id for %s; empty book",
                        listing.key.market_id,
                    )
                    books.append(_empty_book(listing.key, now))
                    continue
                try:
                    resp = await client.get("/book", params={"token_id": token_id})
                    resp.raise_for_status()
                    books.append(_to_book(listing.key, resp.json(), now))
                except Exception:
                    log.exception("polymarket book failed for %s", listing.key.market_id)
                    books.append(_empty_book(listing.key, now))
        return books

    async def discover(self) -> list[UnmappedMarket]:
        now = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(base_url=GAMMA, timeout=15) as client:
                resp = await client.get("/markets", params={"limit": 200})
                resp.raise_for_status()
                markets = resp.json()
        except Exception:
            log.exception("polymarket discovery failed")
            return []
        out: list[UnmappedMarket] = []
        for m in markets if isinstance(markets, list) else []:
            market_id = str(m.get("condition_id") or m.get("id") or "")
            if market_id:
                out.append(
                    UnmappedMarket(
                        venue_id=self.venue_id,
                        market_id=market_id,
                        label=str(m.get("question", "")),
                        seen_at=now,
                        reason="unregistered market",
                    )
                )
        return out
