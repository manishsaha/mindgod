"""SQLite persistence: append-only observations, opportunities, fills.

Quotes are immutable facts stamped with an Observation (valid_at at the
venue, recorded_at by us). We never update a stored quote, so as_of can
reconstruct what we knew at any moment for honest backtests and CLV.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from mindgod.application.calls import (
    Call,
    CallGrade,
    CallSnapshot,
    ClosingLine,
    ManualFill,
    PaperFill,
    Settlement,
)
from mindgod.application.opportunities import Opportunity
from mindgod.application.ports import Fill, ObservationStore, PricedOutcome
from mindgod.application.pricing import outcome_key
from mindgod.domain.quotes import OrderBook
from mindgod.domain.venues import Listing


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
            "CREATE INDEX IF NOT EXISTS obs_lookup ON observations(outcome, recorded_at)"
        )
        # ADR-0008: alert-first paper tracking. All tables append-only.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS calls(
                 call_id TEXT PRIMARY KEY,
                 created_at TEXT NOT NULL,
                 venue TEXT NOT NULL,
                 market_id TEXT NOT NULL,
                 side TEXT NOT NULL,
                 outcome TEXT NOT NULL,
                 fair_prob REAL NOT NULL,
                 fair_se REAL NOT NULL,
                 fair_method TEXT NOT NULL,
                 limit_price_x REAL NOT NULL,
                 contracts_n INTEGER NOT NULL,
                 ask_at_alert REAL NOT NULL,
                 net_edge_at_alert REAL NOT NULL,
                 depth_at_x INTEGER NOT NULL,
                 event_start TEXT)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS call_snapshots(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 call_id TEXT NOT NULL,
                 offset_s INTEGER NOT NULL,
                 best_ask REAL,
                 best_bid REAL,
                 depth_at_x INTEGER NOT NULL,
                 recorded_at TEXT NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_fills(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 call_id TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 contracts INTEGER NOT NULL,
                 avg_price REAL,
                 fee REAL,
                 filled INTEGER NOT NULL,
                 at TEXT NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS manual_fills(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 call_id TEXT NOT NULL,
                 contracts INTEGER NOT NULL,
                 price REAL NOT NULL,
                 fee REAL NOT NULL,
                 taken_at TEXT NOT NULL,
                 note TEXT NOT NULL DEFAULT '')"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS closing_lines(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 outcome_key TEXT NOT NULL,
                 sharp_close_prob REAL NOT NULL,
                 source TEXT NOT NULL,
                 captured_at TEXT NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS settlements(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 outcome_key TEXT NOT NULL,
                 result TEXT NOT NULL,
                 settled_at TEXT NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS call_grades(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 call_id TEXT NOT NULL,
                 clv_reaction REAL,
                 clv_manual REAL,
                 pnl_reaction REAL,
                 pnl_manual REAL,
                 edge_half_life_s REAL,
                 graded_at TEXT NOT NULL)"""
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

    def record_books(self, books: list[tuple[Listing, OrderBook]], recorded_at: datetime) -> None:
        for listing, book in books:
            key = outcome_key(listing.outcome)
            for side, levels in (("ask", book.asks), ("bid", book.bids)):
                for level in levels:
                    self._insert_observation(
                        recorded_at,
                        kind="book",
                        venue=str(book.listing.venue_id),
                        market_id=book.listing.market_id,
                        side=f"{book.listing.side}:{side}",
                        outcome=key,
                        price=float(level.price.dollars),
                        contracts=level.contracts,
                        valid_at=book.observed.valid_at,
                    )
        self._conn.commit()

    def record_priced(self, priced: list[PricedOutcome], recorded_at: datetime) -> None:
        for p in priced:
            self._insert_observation(
                recorded_at,
                kind="quote",
                venue=str(p.listing_key.venue_id),
                market_id=p.listing_key.market_id,
                side=p.listing_key.side,
                outcome=outcome_key(p.outcome),
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
                outcome_key(opportunity.outcome),
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
                outcome_key(fill.outcome),
                fill.contracts,
                float(fill.fill_price),
                float(fill.fee),
                int(fill.live),
            ),
        )
        self._conn.commit()

    def record_call(self, call: Call) -> None:
        """ADR-0008: append a call when an alert fires."""
        self._conn.execute(
            "INSERT INTO calls (call_id, created_at, venue, market_id, side,"
            " outcome, fair_prob, fair_se, fair_method, limit_price_x,"
            " contracts_n, ask_at_alert, net_edge_at_alert, depth_at_x,"
            " event_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.call_id,
                call.created_at.isoformat(),
                str(call.listing_key.venue_id),
                call.listing_key.market_id,
                call.listing_key.side,
                outcome_key(call.outcome),
                call.fair_prob,
                call.fair_se,
                call.fair_method,
                float(call.limit_price_x),
                call.contracts_n,
                float(call.ask_at_alert),
                float(call.net_edge_at_alert),
                call.depth_at_x,
                call.event_start.isoformat() if call.event_start else None,
            ),
        )
        self._conn.commit()

    def record_call_snapshot(self, snap: CallSnapshot) -> None:
        self._conn.execute(
            "INSERT INTO call_snapshots (call_id, offset_s, best_ask, best_bid,"
            " depth_at_x, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                snap.call_id,
                snap.offset_s,
                float(snap.best_ask) if snap.best_ask is not None else None,
                float(snap.best_bid) if snap.best_bid is not None else None,
                snap.depth_at_x,
                snap.recorded_at.isoformat(),
            ),
        )
        self._conn.commit()

    def record_paper_fill(self, fill: PaperFill) -> None:
        self._conn.execute(
            "INSERT INTO paper_fills (call_id, kind, contracts, avg_price, fee,"
            " filled, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fill.call_id,
                fill.kind,
                fill.contracts,
                float(fill.avg_price) if fill.avg_price is not None else None,
                float(fill.fee) if fill.fee is not None else None,
                int(fill.filled),
                fill.at.isoformat(),
            ),
        )
        self._conn.commit()

    def record_manual_fill(self, fill: ManualFill) -> None:
        """ADR-0008: log a fill the user took by hand against a call."""
        self._conn.execute(
            "INSERT INTO manual_fills (call_id, contracts, price, fee,"
            " taken_at, note) VALUES (?, ?, ?, ?, ?, ?)",
            (
                fill.call_id,
                fill.contracts,
                float(fill.price),
                float(fill.fee),
                fill.taken_at.isoformat(),
                fill.note,
            ),
        )
        self._conn.commit()

    def record_closing_line(self, line: ClosingLine) -> None:
        """ADR-0008: last sharp consensus before the event locks."""
        self._conn.execute(
            "INSERT INTO closing_lines (outcome_key, sharp_close_prob, source,"
            " captured_at) VALUES (?, ?, ?, ?)",
            (
                line.outcome_key,
                line.sharp_close_prob,
                line.source,
                line.captured_at.isoformat(),
            ),
        )
        self._conn.commit()

    def record_settlement(self, settlement: Settlement) -> None:
        self._conn.execute(
            "INSERT INTO settlements (outcome_key, result, settled_at) VALUES (?, ?, ?)",
            (
                settlement.outcome_key,
                settlement.result,
                settlement.settled_at.isoformat(),
            ),
        )
        self._conn.commit()

    def record_call_grade(self, grade: CallGrade) -> None:
        """ADR-0008: CLV/P&L/half-life for a call, after close and settlement."""
        self._conn.execute(
            "INSERT INTO call_grades (call_id, clv_reaction, clv_manual,"
            " pnl_reaction, pnl_manual, edge_half_life_s, graded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                grade.call_id,
                grade.clv_reaction,
                grade.clv_manual,
                grade.pnl_reaction,
                grade.pnl_manual,
                grade.edge_half_life_s,
                grade.graded_at.isoformat(),
            ),
        )
        self._conn.commit()

    def as_of(self, key: str, ts: datetime) -> list[tuple[Any, ...]]:
        """Latest observation rows known at `ts` for one outcome key."""
        return self._conn.execute(
            "SELECT venue, market_id, side, price, contracts, valid_at,"
            " recorded_at FROM observations"
            " WHERE outcome = ? AND recorded_at <= ?"
            " ORDER BY recorded_at DESC LIMIT 50",
            (key, ts.isoformat()),
        ).fetchall()
