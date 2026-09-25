"""SQLite persistence: append-only observations, opportunities, fills.

Quotes are immutable facts stamped with an Observation (valid_at at the
venue, recorded_at by us). We never update a stored quote, so as_of can
reconstruct what we knew at any moment for honest backtests and CLV.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from mindgod.application.opportunities import Opportunity
from mindgod.application.ports import Fill, ObservationStore, PricedOutcome
from mindgod.domain.quotes import OrderBook


class Store(ObservationStore):
    def __init__(self, path: str = "mindgod.db") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS observations(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 recorded_at TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 venue TEXT NOT NULL,
                 market_id TEXT NOT NULL,
                 side TEXT NOT NULL,
                 outcome TEXT NOT NULL,
                 price REAL NOT NULL,
                 contracts INTEGER NOT NULL,
                 valid_at TEXT NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS opportunities(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 at TEXT NOT NULL,
                 venue TEXT NOT NULL,
                 market_id TEXT NOT NULL,
                 side TEXT NOT NULL,
                 outcome TEXT NOT NULL,
                 fair_prob REAL NOT NULL,
                 fair_se REAL NOT NULL,
                 method TEXT NOT NULL,
                 avg_price REAL NOT NULL,
                 contracts INTEGER NOT NULL,
                 edge_net REAL NOT NULL,
                 fee REAL NOT NULL,
                 stake REAL NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fills(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 at TEXT NOT NULL,
                 venue TEXT NOT NULL,
                 market_id TEXT NOT NULL,
                 side TEXT NOT NULL,
                 outcome TEXT NOT NULL,
                 contracts INTEGER NOT NULL,
                 fill_price REAL NOT NULL,
                 fee REAL NOT NULL,
                 live INTEGER NOT NULL)"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS obs_lookup ON observations"
            "(outcome, recorded_at)"
        )
        self._conn.commit()

    def _insert_observation(
        self,
        recorded_at: datetime,
        kind: str,
        venue: str,
        market_id: str,
        side: str,
        outcome: str,
        price: float,
        contracts: int,
        valid_at: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO observations (recorded_at, kind, venue, market_id,"
            " side, outcome, price, contracts, valid_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recorded_at.isoformat(),
                kind,
                venue,
                market_id,
                side,
                outcome,
                price,
                contracts,
                valid_at.isoformat(),
            ),
        )

    def record_books(self, books: list[OrderBook], recorded_at: datetime) -> None:
        for book in books:
            for side, levels in (("ask", book.asks), ("bid", book.bids)):
                for level in levels:
                    self._insert_observation(
                        recorded_at,
                        kind="book",
                        venue=str(book.listing.venue_id),
                        market_id=book.listing.market_id,
                        side=f"{book.listing.side}:{side}",
                        outcome="",
                        price=float(level.price.dollars),
                        contracts=level.contracts,
                        valid_at=book.observed.valid_at,
                    )
        self._conn.commit()

    def record_priced(
        self, priced: list[PricedOutcome], recorded_at: datetime
    ) -> None:
        for p in priced:
            self._insert_observation(
                recorded_at,
                kind="quote",
                venue=str(p.listing_key.venue_id),
                market_id=p.listing_key.market_id,
                side=p.listing_key.side,
                outcome=repr(p.outcome),
                price=p.quote.implied_probability.value,
                contracts=0,
                valid_at=p.quote.observed.valid_at,
            )
        self._conn.commit()

    def record_opportunity(self, opportunity: Opportunity, at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO opportunities (at, venue, market_id, side, outcome,"
            " fair_prob, fair_se, method, avg_price, contracts, edge_net, fee,"
            " stake) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                at.isoformat(),
                str(opportunity.listing.venue_id),
                opportunity.listing.market_id,
                opportunity.listing.side,
                repr(opportunity.outcome),
                opportunity.fair_value.probability.value,
                opportunity.fair_value.standard_error,
                opportunity.fair_value.method,
                float(opportunity.fill.average_price),
                opportunity.fill.contracts,
                float(opportunity.edge_net),
                float(opportunity.fee),
                float(opportunity.stake),
            ),
        )
        self._conn.commit()

    def record_fill(self, fill: Fill) -> None:
        self._conn.execute(
            "INSERT INTO fills (at, venue, market_id, side, outcome,"
            " contracts, fill_price, fee, live) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.at.isoformat(),
                str(fill.listing.venue_id),
                fill.listing.market_id,
                fill.listing.side,
                repr(fill.outcome),
                fill.contracts,
                float(fill.fill_price),
                float(fill.fee),
                int(fill.live),
            ),
        )
        self._conn.commit()

    def as_of(self, outcome_repr: str, ts: datetime) -> list[tuple[Any, ...]]:
        """Latest observation rows known at `ts` for one outcome repr."""
        return self._conn.execute(
            "SELECT venue, market_id, side, price, contracts, valid_at,"
            " recorded_at FROM observations"
            " WHERE outcome = ? AND recorded_at <= ?"
            " ORDER BY recorded_at DESC LIMIT 50",
            (outcome_repr, ts.isoformat()),
        ).fetchall()
