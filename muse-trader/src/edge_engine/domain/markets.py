"""Canonical markets and outcomes.

A canonical market is one proposition about one event, e.g. "Chiefs moneyline
vs Bills on 2026-10-05". Every venue's version of that proposition links here
through the mapper, with its settlement rules attached.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .settlement import SettlementRules

MARKET_TYPES = ("moneyline", "spread", "total", "prop", "combo", "other")
MARKET_STATUSES = ("open", "suspended", "closed", "settled")


@dataclass(frozen=True)
class Outcome:
    outcome_id: str          # home | away | over | under | yes | no ...
    label: str
    line: float | None = None  # spread / total / prop threshold


@dataclass
class CanonicalMarket:
    market_id: str           # e.g. nfl:KC-BUF:20261005:moneyline
    event_id: str
    label: str               # human-readable: "Chiefs vs Bills"
    market_type: str = "other"
    outcomes: list[Outcome] = field(default_factory=list)
    settlement: SettlementRules = field(default_factory=SettlementRules)
    status: str = "open"
    starts_at: datetime | None = None
    combo_legs: tuple[str, ...] = ()  # canonical market ids, for combo markets
