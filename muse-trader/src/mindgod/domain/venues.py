"""Venues and listings: where a canonical outcome is actually tradable."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

from mindgod.domain.propositions import Outcome
from mindgod.domain.terms import Terms, differences

VenueId = NewType("VenueId", str)


class VenueKind(StrEnum):
    EXCHANGE = "exchange"  # peer-to-peer order book: no limits, commission on trades
    SPORTSBOOK = "sportsbook"  # house prices with vig: limits winners


@dataclass(frozen=True, slots=True)
class Venue:
    id: VenueId
    kind: VenueKind


@dataclass(frozen=True, slots=True)
class ListingKey:
    """A venue's own address for one tradable side, e.g. a Kalshi ticker plus 'yes'."""

    venue_id: VenueId
    market_id: str
    side: str


@dataclass(frozen=True, slots=True)
class Listing:
    """Maps a venue market onto a canonical outcome plus that venue's settlement terms."""

    key: ListingKey
    terms: Terms

    @property
    def outcome(self) -> Outcome:
        return self.terms.payoff.wins_if

    def is_same_bet_as(self, other: Listing) -> bool:
        return not differences(self.terms, other.terms)
