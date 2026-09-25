"""Sports reference data: leagues, teams, players, events, and game periods."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

TeamId = NewType("TeamId", str)
PlayerId = NewType("PlayerId", str)
EventId = NewType("EventId", str)


class League(StrEnum):
    MLB = "mlb"
    NFL = "nfl"


class Period(StrEnum):
    """The slice of a game a quantity is measured over.

    FULL_GAME includes overtime / extra innings; REGULATION does not. They are
    different random variables, so they are different periods, not a
    settlement detail.
    """

    FULL_GAME = "full_game"
    REGULATION = "regulation"
    FIRST_HALF = "first_half"
    FIRST_QUARTER = "first_quarter"
    FIRST_FIVE_INNINGS = "first_five_innings"
    FIRST_INNING = "first_inning"


PERIODS_BY_LEAGUE: dict[League, frozenset[Period]] = {
    League.NFL: frozenset(
        {Period.FULL_GAME, Period.REGULATION, Period.FIRST_HALF, Period.FIRST_QUARTER}
    ),
    League.MLB: frozenset(
        {Period.FULL_GAME, Period.REGULATION, Period.FIRST_FIVE_INNINGS, Period.FIRST_INNING}
    ),
}


class EventStatus(StrEnum):
    """Lifecycle of a game. Status changes are recorded as separate facts over time."""

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Team:
    id: TeamId
    league: League
    name: str
    abbreviation: str


@dataclass(frozen=True, slots=True)
class Player:
    id: PlayerId
    league: League
    full_name: str


@dataclass(frozen=True, slots=True)
class Event:
    """A single game.

    Identity is fixed at creation. Start-time and status changes (rain delays,
    NFL flex scheduling, postponements) are recorded as separate facts.
    """

    id: EventId
    league: League
    home: TeamId
    away: TeamId
    scheduled_start: datetime
    game_number: int = 1  # MLB doubleheaders: same teams, same date, two events

    def __post_init__(self) -> None:
        if self.home == self.away:
            raise ValueError("a team cannot play itself")
        if self.scheduled_start.tzinfo is None:
            raise ValueError("scheduled_start must be timezone-aware")
        if self.game_number < 1:
            raise ValueError("game_number starts at 1")

    def involves(self, team: TeamId) -> bool:
        return team in (self.home, self.away)
