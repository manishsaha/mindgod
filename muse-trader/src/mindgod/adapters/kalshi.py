"""Kalshi: public REST for quotes.

Market-data endpoints need no auth. Order placement needs request signing
(RSA-PSS or Ed25519) and is not wired: the live execution venue fails closed.

The order book is bids only: yes_dollars and no_dollars each hold resting
bids, ascending, best bid last. A No bid at q is a Yes ask at 1 - q, so
buying Yes lifts No bids and buying No lifts Yes bids. Prices and counts are
fixed-point strings; counts may be fractional and are floored to whole
contracts (a level flooring to zero is dropped, and a side with no bids
simply has no asks: no book, no trade).

Endpoints (verified 2026-09-25 against production):
  GET {BASE}/markets
  GET {BASE}/markets/{ticker}/orderbook
    -> {"orderbook_fp": {"yes_dollars": [["0.4200", "13.00"], ...],
                         "no_dollars": [...]}}
  GET {BASE}/markets/orderbooks?tickers=A&tickers=B   (1-100 per request)
    -> {"orderbooks": [{"ticker": "A", "orderbook_fp": {...}}, ...]}
    Tickers are repeated params; the comma-joined form silently returns an
    empty book, so it is never used.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from mindgod.application.ports import ExchangeSource, UnmappedMarket
from mindgod.domain.primitives import ContractPrice, Observation
from mindgod.domain.quotes import OrderBook, PriceLevel
from mindgod.domain.venues import Listing, ListingKey, VenueId

log = logging.getLogger("mindgod.adapters.kalshi")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
BATCH_LIMIT = 100


def _parse_bid_levels(raw: Any) -> list[tuple[Decimal, int]]:
    """Raw [[price, count], ...] bid levels -> (price dollars, whole contracts).

    Accepts the documented fixed-point dollar strings and the legacy
    integer-cents form (a price >= 1 is read as cents).
    """
    levels: list[tuple[Decimal, int]] = []
    if not isinstance(raw, list):
        return levels
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        try:
            price = Decimal(str(entry[0]))
            if price >= 1:
                price = price / Decimal(100)  # legacy integer cents
            size = int(Decimal(str(entry[1])))  # fractional counts floor
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not Decimal(0) < price < Decimal(1):
            continue
        if size <= 0:
            continue
        levels.append((price, size))
    return levels


def _extract_bids(
    payload: dict[str, Any],
) -> tuple[list[tuple[Decimal, int]], list[tuple[Decimal, int]]]:
    fp = payload.get("orderbook_fp")
    if isinstance(fp, dict):
        return (
            _parse_bid_levels(fp.get("yes_dollars")),
            _parse_bid_levels(fp.get("no_dollars")),
        )
    legacy = payload.get("orderbook")
    if isinstance(legacy, dict):
        return (
            _parse_bid_levels(legacy.get("yes")),
            _parse_bid_levels(legacy.get("no")),
        )
    return [], []


def _ask_levels(bids: list[tuple[Decimal, int]]) -> tuple[PriceLevel, ...]:
    """Bids on one side are asks at 1 - price on the other side, best first."""
    return tuple(
        sorted(
            (PriceLevel(ContractPrice(Decimal(1) - price), size) for price, size in bids),
            key=lambda lvl: lvl.price.dollars,
        )
    )


def _bid_levels(bids: list[tuple[Decimal, int]]) -> tuple[PriceLevel, ...]:
    return tuple(
        sorted(
            (PriceLevel(ContractPrice(price), size) for price, size in bids),
            key=lambda lvl: lvl.price.dollars,
            reverse=True,
        )
    )


def _to_book(key: ListingKey, payload: dict[str, Any], now: datetime) -> OrderBook:
    yes_bids, no_bids = _extract_bids(payload)
    if key.side == "no":
        asks, bids = _ask_levels(yes_bids), _bid_levels(no_bids)
    else:
        asks, bids = _ask_levels(no_bids), _bid_levels(yes_bids)
    return OrderBook(listing=key, asks=asks, bids=bids, observed=Observation(now, now))


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class KalshiExchange(ExchangeSource):
    venue_id = VenueId("kalshi")

    def __init__(self, series_tickers: tuple[str, ...] = ("KXNFLGAME", "KXMLBGAME")) -> None:
        # Discovery only feeds the human review queue, but an unfiltered
        # /markets pull returns every kind of market. Restrict to the series
        # we actually price.
        self._series_tickers = series_tickers

    async def order_books(self, listings: list[Listing]) -> list[OrderBook]:
        now = datetime.now(UTC)
        if not listings:
            return []
        tickers = list(dict.fromkeys(x.key.market_id for x in listings))
        payloads: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
            for chunk in _chunks(tickers, BATCH_LIMIT):
                payloads.update(await self._batch_books(client, chunk))
                for ticker in chunk:
                    if ticker not in payloads:
                        single = await self._single_book(client, ticker)
                        if single is not None:
                            payloads[ticker] = single
        return [_to_book(lst.key, payloads.get(lst.key.market_id, {}), now) for lst in listings]

    async def _batch_books(
        self, client: httpx.AsyncClient, tickers: list[str]
    ) -> dict[str, dict[str, Any]]:
        try:
            resp = await client.get(
                "/markets/orderbooks",
                params=[("tickers", t) for t in tickers],
            )
            resp.raise_for_status()
            entries = resp.json().get("orderbooks", [])
        except Exception:
            log.exception("kalshi batch orderbook failed")
            return {}
        out: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if isinstance(entry, dict) and entry.get("ticker"):
                out[str(entry["ticker"])] = entry
        return out

    async def _single_book(self, client: httpx.AsyncClient, ticker: str) -> dict[str, Any] | None:
        try:
            resp = await client.get(f"/markets/{ticker}/orderbook")
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            log.exception("kalshi orderbook failed for %s", ticker)
            return None

    async def discover(self) -> list[UnmappedMarket]:
        now = datetime.now(UTC)
        markets: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
                for series in self._series_tickers:
                    resp = await client.get(
                        "/markets", params={"limit": 200, "series_ticker": series}
                    )
                    resp.raise_for_status()
                    batch = resp.json().get("markets", [])
                    markets.extend(batch if isinstance(batch, list) else [])
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
