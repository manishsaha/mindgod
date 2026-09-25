"""Ports: interfaces the application needs from the outside world.

Adapters implement these protocols. The application depends only on these
and the domain, never on concrete adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from mindgod.domain.propositions import Outcome
from mindgod.domain.quotes import OrderBook, SportsbookQuote
from mindgod.domain.terms import Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing, ListingKey, VenueId

if TYPE_CHECKING:
    from .opportunities import Opportunity


@dataclass(frozen=True, slots=True)
class PricedOutcome:
    """A sportsbook price already translated to a canonical outcome.

    Adapters build the outcome with the builders in domain.propositions, so
    equality with the outcomes that listings point at is structural. `terms`
    carries the book's settlement terms (push/tie refunds, void policy): a
    fair value may only use prices whose terms match the listing's, because
    the same canonical outcome with different refund rules is a different bet.
    """

    outcome: Outcome
    listing_key: ListingKey
    quote: SportsbookQuote
    market_group: str  # outcomes devigged together, e.g. "draftkings:nfl-kc-buf:h2h"
    terms: Terms


@dataclass(frozen=True, slots=True)
class UnmappedMarket:
    """A venue market we saw but cannot translate yet. Human review only:
    never auto-traded."""

    venue_id: VenueId
    market_id: str
    label: str
    seen_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class Fill:
    listing: ListingKey
    outcome: Outcome
    contracts: int
    fill_price: Decimal
    fee: Decimal
    live: bool
    at: datetime


class SportsbookSource(Protocol):
    async def priced_outcomes(self) -> list[PricedOutcome]: ...


class ExchangeSource(Protocol):
    venue_id: VenueId

    async def order_books(self, listings: list[Listing]) -> list[OrderBook]:
        """One OrderBook per listing, in input order.

        A listing whose book cannot be fetched yields an empty book (no
        asks, no bids): no book, no trade. Adapters must keep the alignment;
        the service pairs them positionally.
        """
        ...

    async def discover(self) -> list[UnmappedMarket]: ...


class ListingResolver(Protocol):
    def register(self, listing: Listing) -> None: ...
    def resolve(self, key: ListingKey) -> Listing | None: ...
    def listings_for(self, venue_id: VenueId) -> list[Listing]: ...
    def report_unmapped(self, market: UnmappedMarket) -> None: ...
    def event_start(self, key: ListingKey) -> datetime | None: ...

    @property
    def review_queue(self) -> list[UnmappedMarket]: ...


class FairValueModel(Protocol):
    def value(self, priced: list[PricedOutcome], as_of: datetime) -> dict[Outcome, FairValue]: ...


class OpportunityDetector(Protocol):
    def detect(
        self,
        books: list[tuple[Listing, OrderBook]],
        fair_values: dict[Outcome, FairValue],
    ) -> list[Opportunity]: ...


class ObservationStore(Protocol):
    def record_books(
        self, books: list[tuple[Listing, OrderBook]], recorded_at: datetime
    ) -> None: ...
    def record_priced(self, priced: list[PricedOutcome], recorded_at: datetime) -> None: ...
    def record_opportunity(self, opportunity: Opportunity, at: datetime) -> None: ...
    def record_fill(self, fill: Fill) -> None: ...


class ExecutionVenue(Protocol):
    name: str

    async def buy(self, opportunity: Opportunity) -> Fill | None: ...


class Notifier(Protocol):
    async def send(self, opportunity: Opportunity) -> bool: ...
