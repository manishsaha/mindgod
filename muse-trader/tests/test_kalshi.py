"""Kalshi parser tests, against a captured production response.

The fixture is a real payload from GET
/trade-api/v2/markets/KXNFLGAME-26OCT05ATLNO-NO/orderbook (2026-09-25),
saved verbatim: fixed-point dollar strings, fractional counts, bids only.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from mindgod.adapters import kalshi
from mindgod.domain.primitives import ContractPrice
from mindgod.domain.quotes import PriceLevel
from mindgod.domain.venues import ListingKey, VenueId

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi_orderbook_fp.json"
NOW = datetime(2026, 9, 25, tzinfo=UTC)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _no_listing() -> ListingKey:
    return ListingKey(VenueId("kalshi"), "KXNFLGAME-26OCT05ATLNO-NO", "no")


def _yes_listing() -> ListingKey:
    return ListingKey(VenueId("kalshi"), "KXNFLGAME-26OCT05ATLNO-NO", "yes")


def test_fixture_is_bid_only_and_fixed_point():
    fp = _fixture()["orderbook_fp"]
    assert set(fp) == {"yes_dollars", "no_dollars"}  # no ask ladder exists
    assert fp["no_dollars"][-1] == ["0.3800", "2151.24"]  # best bid last, fractional
    assert fp["yes_dollars"][-1] == ["0.6100", "32.53"]


def test_no_listing_bids_from_no_ladder():
    book = kalshi._to_book(_no_listing(), _fixture(), NOW)
    assert book.bids[0].price.dollars == Decimal("0.38")
    assert book.bids[0].contracts == 2151  # fractional count floors
    assert book.bids[1].price.dollars == Decimal("0.37")
    assert book.asks[0].price.dollars == Decimal("0.39")  # 1 - best yes bid
    assert book.asks[0].contracts == 32
    # Every level floors, never rounds up.
    for level in book.asks + book.bids:
        assert level.contracts > 0
    prices = [lvl.price.dollars for lvl in book.asks]
    assert prices == sorted(prices)
    prices = [lvl.price.dollars for lvl in book.bids]
    assert prices == sorted(prices, reverse=True)


def test_yes_listing_bids_from_yes_ladder():
    book = kalshi._to_book(_yes_listing(), _fixture(), NOW)
    assert book.bids[0].price.dollars == Decimal("0.61")
    assert book.bids[0].contracts == 32
    assert book.asks[0].price.dollars == Decimal("0.62")  # 1 - best no bid


def test_critique_example_yes_bids_45_48_no_bid_50():
    """The bug the parser fixes: yes bids at 45/48 with a no bid at 50 make
    the yes ask 50, not 48."""
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.4500", "10.00"], ["0.4800", "5.00"]],
            "no_dollars": [["0.5000", "12.00"]],
        }
    }
    book = kalshi._to_book(_yes_listing(), payload, NOW)
    assert book.asks[0].price.dollars == Decimal("0.50")
    assert book.bids[0].price.dollars == Decimal("0.48")


def test_legacy_envelope_still_parses():
    payload = {
        "orderbook": {"yes": [[45, 10]], "no": [[50, 12]]},  # integer cents
    }
    book = kalshi._to_book(_yes_listing(), payload, NOW)
    assert book.bids[0].price.dollars == Decimal("0.45")
    assert book.asks[0].price.dollars == Decimal("0.50")


def test_empty_payload_is_an_empty_book():
    book = kalshi._to_book(_yes_listing(), {}, NOW)
    assert book.asks == () and book.bids == ()


def test_malformed_levels_are_skipped():
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.4800", "5.00"], ["junk", "x"], "nope", [42]],
            "no_dollars": [],
        }
    }
    book = kalshi._to_book(_yes_listing(), payload, NOW)
    assert book.bids == (PriceLevel(ContractPrice(Decimal("0.48")), 5),)
    assert book.asks == ()


def test_batch_params_use_repeated_tickers():
    seen: dict = {}

    class FakeResp:
        def raise_for_status(self): ...
        def json(self):
            return {"orderbooks": []}

    class FakeClient:
        async def get(self, path, params=None):
            seen["path"] = path
            seen["params"] = params
            return FakeResp()

    exchange = kalshi.KalshiExchange()
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        exchange._batch_books(FakeClient(), ["A", "B"])  # noqa: SLF001
    )
    assert seen["path"] == "/markets/orderbooks"
    assert seen["params"] == [("tickers", "A"), ("tickers", "B")]


def test_discovery_filters_by_series(monkeypatch):
    seen: list = []

    class FakeResp:
        def raise_for_status(self): ...
        def json(self):
            return {"markets": [{"ticker": "T1", "title": "game"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path, params=None):
            seen.append((path, params))
            return FakeResp()

    monkeypatch.setattr(kalshi.httpx, "AsyncClient", FakeClient)
    exchange = kalshi.KalshiExchange(series_tickers=("KXNFLGAME", "KXMLBGAME"))
    import asyncio

    out = asyncio.new_event_loop().run_until_complete(exchange.discover())
    assert seen == [
        ("/markets", {"limit": 200, "series_ticker": "KXNFLGAME"}),
        ("/markets", {"limit": 200, "series_ticker": "KXMLBGAME"}),
    ]
    assert [m.market_id for m in out] == ["T1", "T1"]
