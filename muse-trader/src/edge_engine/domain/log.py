"""Append-only, bitemporal quote log.

Every observed quote is stored with both timestamps. as_of() answers "what
did we know at time T", which is what makes backtests honest and lets us
compute closing line value: compare our fill price against the sharp
consensus at close, not against what we know now.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .quotes import Quote


class QuoteLog:
    def __init__(self, path: str = "quotes.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS quotes(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 canonical_market_id TEXT,
                 venue TEXT NOT NULL,
                 venue_market_id TEXT NOT NULL,
                 outcome_id TEXT NOT NULL,
                 side TEXT NOT NULL,
                 price REAL NOT NULL,
                 size REAL,
                 valid_at TEXT,
                 observed_at TEXT NOT NULL)"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_quotes_lookup ON quotes
               (canonical_market_id, venue, outcome_id, side, observed_at)"""
        )
        self.conn.commit()

    def append(self, quote: Quote) -> None:
        self.conn.execute(
            """INSERT INTO quotes
               (canonical_market_id, venue, venue_market_id, outcome_id, side,
                price, size, valid_at, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote.canonical_market_id,
                quote.venue,
                quote.venue_market_id,
                quote.outcome_id,
                quote.side,
                quote.price,
                quote.size,
                quote.valid_at.isoformat() if quote.valid_at else None,
                quote.observed_at.isoformat() if quote.observed_at else None,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_quote(row: tuple) -> Quote:
        (mid, venue, vmid, outcome, side, price, size,
         valid_at, observed_at) = row
        return Quote(
            canonical_market_id=mid,
            venue=venue,
            venue_market_id=vmid,
            outcome_id=outcome,
            side=side,
            price=price,
            size=size,
            valid_at=datetime.fromisoformat(valid_at) if valid_at else None,
            observed_at=datetime.fromisoformat(observed_at) if observed_at else None,
        )

    def as_of(
        self, canonical_market_id: str, ts: datetime
    ) -> dict[tuple[str, str, str], Quote]:
        """Latest quote per (venue, outcome, side) observed at or before ts."""
        rows = self.conn.execute(
            """SELECT canonical_market_id, venue, venue_market_id, outcome_id,
                      side, price, size, valid_at, observed_at
               FROM quotes
               WHERE canonical_market_id = ? AND observed_at <= ?
               ORDER BY observed_at ASC""",
            (canonical_market_id, ts.isoformat()),
        ).fetchall()
        latest: dict[tuple[str, str, str], Quote] = {}
        for row in rows:
            q = self._row_to_quote(row)
            latest[(q.venue, q.outcome_id, q.side)] = q
        return latest

    def history(
        self,
        canonical_market_id: str,
        venue: str,
        outcome_id: str,
        side: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Quote]:
        """Full observed price path for one quote series, oldest first."""
        query = """SELECT canonical_market_id, venue, venue_market_id, outcome_id,
                          side, price, size, valid_at, observed_at
                   FROM quotes
                   WHERE canonical_market_id = ? AND venue = ?
                     AND outcome_id = ? AND side = ?"""
        params: list = [canonical_market_id, venue, outcome_id, side]
        if start is not None:
            query += " AND observed_at >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND observed_at <= ?"
            params.append(end.isoformat())
        query += " ORDER BY observed_at ASC"
        return [self._row_to_quote(r)
                for r in self.conn.execute(query, params).fetchall()]
