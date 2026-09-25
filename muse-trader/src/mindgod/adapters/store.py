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

    def latest_quotes_before(
        self, outcome_key: str, before: datetime
    ) -> list[tuple[str, float, str]]:
        """Get the latest quote per venue for an outcome with valid_at before the given time.

        Returns list of (venue, price, valid_at) tuples. Used for closing-line
        capture: the last pre-game consensus, not in-game prices.
        """
        rows = self._conn.execute(
            """SELECT venue, price, valid_at, MAX(recorded_at)
               FROM observations
               WHERE kind = 'quote' AND outcome = ? AND valid_at < ?
               GROUP BY venue""",
            (outcome_key, before.isoformat()),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def has_closing_line(self, outcome_key: str) -> bool:
        """Check if a closing line has already been captured for an outcome."""
        row = self._conn.execute(
            "SELECT 1 FROM closing_lines WHERE outcome_key = ? LIMIT 1",
            (outcome_key,),
        ).fetchone()
        return row is not None

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

    def unsettled_calls(self) -> list[tuple[str, str, str, str]]:
        """Get calls that need settlement: (call_id, market_id, side, outcome_key)."""
        rows = self._conn.execute(
            """SELECT c.call_id, c.market_id, c.side, c.outcome_key
               FROM calls c
               LEFT JOIN settlements s ON c.outcome_key = s.outcome_key
               WHERE s.outcome_key IS NULL""",
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        """Fetch a call by ID for grading."""
        row = self._conn.execute(
            "SELECT call_id, created_at, venue, market_id, side, outcome, fair_prob,"
            " fair_se, fair_method, limit_price_x, contracts_n, ask_at_alert,"
            " outcome_key FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "call_id": row[0],
            "created_at": row[1],
            "venue": row[2],
            "market_id": row[3],
            "side": row[4],
            "outcome": row[5],
            "fair_prob": row[6],
            "fair_se": row[7],
            "fair_method": row[8],
            "limit_price_x": row[9],
            "contracts_n": row[10],
            "ask_at_alert": row[11],
            "outcome_key": row[12],
        }

    def get_paper_fill(self, call_id: str) -> dict[str, Any] | None:
        """Fetch the latest paper fill for a call."""
        row = self._conn.execute(
            "SELECT fill_id, call_id, filled_at, price, contracts, kind"
            " FROM paper_fills WHERE call_id = ? ORDER BY filled_at DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "fill_id": row[0],
            "call_id": row[1],
            "filled_at": row[2],
            "price": row[3],
            "contracts": row[4],
            "kind": row[5],
        }

    def get_manual_fill(self, call_id: str) -> dict[str, Any] | None:
        """Fetch the manual fill for a call, if any."""
        row = self._conn.execute(
            "SELECT fill_id, call_id, filled_at, price, contracts, noted_at"
            " FROM manual_fills WHERE call_id = ? LIMIT 1",
            (call_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "fill_id": row[0],
            "call_id": row[1],
            "filled_at": row[2],
            "price": row[3],
            "contracts": row[4],
            "noted_at": row[5],
        }

    def get_snapshots(self, call_id: str) -> list[dict[str, Any]]:
        """Fetch all snapshots for a call, ordered by time."""
        rows = self._conn.execute(
            "SELECT call_id, offset_s, best_ask, best_bid, depth_at_x, recorded_at"
            " FROM call_snapshots WHERE call_id = ? ORDER BY offset_s",
            (call_id,),
        ).fetchall()
        return [
            {
                "call_id": r[0],
                "offset_s": r[1],
                "best_ask": r[2],
                "best_bid": r[3],
                "depth_at_x": r[4],
                "recorded_at": r[5],
            }
            for r in rows
        ]

    def get_closing_line(self, outcome_key: str) -> dict[str, Any] | None:
        """Fetch the closing line for an outcome, if captured."""
        row = self._conn.execute(
            "SELECT outcome_key, sharp_close_prob, source, captured_at"
            " FROM closing_lines WHERE outcome_key = ? LIMIT 1",
            (outcome_key,),
        ).fetchone()
        if not row:
            return None
        return {
            "outcome_key": row[0],
            "sharp_close_prob": row[1],
            "source": row[2],
            "captured_at": row[3],
        }

    def as_of(self, key: str, ts: datetime) -> list[tuple[Any, ...]]:
        """Latest observation rows known at `ts` for one outcome key."""
        return self._conn.execute(
            "SELECT venue, market_id, side, price, contracts, valid_at,"
            " recorded_at FROM observations"
            " WHERE outcome = ? AND recorded_at <= ?"
            " ORDER BY recorded_at DESC LIMIT 50",
            (key, ts.isoformat()),
        ).fetchall()
