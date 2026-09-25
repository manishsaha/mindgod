"""Canonical real-world events.

An event is something that happens in the world (a game, an election, a
release). Markets are propositions about the event. Many markets can attach
to one event, and the same event is described differently by every venue,
which is why identity lives here and not in the pollers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class CanonicalEvent:
    event_id: str            # e.g. nfl:KC-BUF:20261005
    sport: str               # football, basketball, politics, ...
    league: str              # nfl, nba, ...
    participants: tuple[str, ...] = ()   # normalized names
    starts_at: datetime | None = None
    venue: str | None = None             # physical venue, when relevant
