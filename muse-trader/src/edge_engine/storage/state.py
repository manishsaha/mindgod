"""Signal/alert/fill persistence. SQLite locally; mirror schema in DynamoDB on AWS."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

from ..engine.ev import Signal


class StateStore:
    def __init__(self, path: str = "edge_engine.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS signals(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts TEXT NOT NULL,
                 event_key TEXT NOT NULL,
                 venue TEXT NOT NULL,
                 side TEXT NOT NULL,
                 market_price REAL NOT NULL,
                 fair_prob REAL NOT NULL,
                 edge REAL NOT NULL,
                 ev_per_dollar REAL NOT NULL,
                 stake REAL NOT NULL,
                 refs TEXT NOT NULL,
                 alerted INTEGER NOT NULL DEFAULT 0,
                 UNIQUE(event_key, venue, side, ts))"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS fills(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts TEXT NOT NULL,
                 event_key TEXT NOT NULL,
                 venue TEXT NOT NULL,
                 side TEXT NOT NULL,
                 contracts REAL NOT NULL,
                 fill_price REAL NOT NULL,
                 live INTEGER NOT NULL)"""
        )
        self.conn.commit()

    def record_signal(self, signal: Signal, alerted: bool) -> None:
        d = asdict(signal)
        self.conn.execute(
            """INSERT OR IGNORE INTO signals
               (ts, event_key, venue, side, market_price, fair_prob, edge,
                ev_per_dollar, stake, refs, alerted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), d["event_key"], d["venue"],
             d["side"], d["market_price"], d["fair_prob"], d["edge"],
             d["ev_per_dollar"], d["stake"], d["refs"], int(alerted)),
        )
        self.conn.commit()

    def record_fill(self, event_key: str, venue: str, side: str, contracts: float,
                    fill_price: float, live: bool) -> None:
        self.conn.execute(
            "INSERT INTO fills (ts, event_key, venue, side, contracts, fill_price, live)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), event_key, venue, side,
             contracts, fill_price, int(live)),
        )
        self.conn.commit()

    def daily_pnl(self) -> float:
        # Placeholder: needs settlement data to close positions. Paper P&L is
        # tracked in PaperBroker until settlement feeds are wired.
        return 0.0

    def export_json(self) -> str:
        rows = self.conn.execute(
            "SELECT ts, event_key, venue, side, edge, stake, alerted FROM signals"
            " ORDER BY ts DESC LIMIT 500"
        ).fetchall()
        return json.dumps(
            [dict(zip(("ts", "event_key", "venue", "side", "edge", "stake",
                       "alerted"), r)) for r in rows],
            indent=2,
        )
