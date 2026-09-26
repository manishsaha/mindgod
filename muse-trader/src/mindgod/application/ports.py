"""Ports: interfaces the application needs from the outside world.

Adapters implement these protocols. The application depends only on these
and the domain, never on concrete adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from mindgod.domain.propositions import Outcome
from mindgod.domain.quotes import OrderBook, SportsbookQuote
from mindgod.domain.terms import Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing, ListingKey, VenueId

if TYPE_CHECKING:
    from .calls import (
        Call,
        CallGrade,
        CallSnapshot,
        ClosingLine,
        ManualFill,
        PaperFill,
        Settlement,
    )
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


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """Credit-burn telemetry from the last sportsbook poll.

    `used` is the x-requests-last header, `remaining` x-requests-remaining.
    `bookmakers` are the bookmaker keys the API actually returned, so a
    poll that silently drops Pinnacle is visible without guessing.
    """

    used: int | None
    remaining: int | None
    bookmakers: tuple[str, ...] = ()


class OpsPoster(Protocol):
    """Fire-and-forget text posts to the ops channel (credit burn, errors)."""

    async def post(self, text: str) -> None: ...


class SportsbookSource(Protocol):
    async def priced_outcomes(self) -> list[PricedOutcome]: ...

    def quota_status(self) -> QuotaStatus | None:
        """Credit-burn telemetry from the last poll, if the source tracks it."""
        ...


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


@dataclass(frozen=True, slots=True)
class ProbablePitchers:
    """Announced starters for an MLB game."""

    event_id: str
    home_pitcher: str | None
    away_pitcher: str | None
    announced_at: datetime


class ProbablePitcherSource(Protocol):
    """ADR-0009: records each game's probable starters.

    When probables change, or fewer than two are announced, MLB listings for
    that game are suppressed until the next sportsbook refresh after the change.
    """

    async def probables(self, event_ids: list[str]) -> list[ProbablePitchers]: ...


class FairValueModel(Protocol):
    def values_by_terms(
        self, priced: list[PricedOutcome], as_of: datetime
    ) -> dict[str, dict[Outcome, FairValue]]: ...

    def consensus(self, pairs: list[tuple[str, float]]) -> float:
        """Sharp-weighted average of (venue_id, probability) pairs.

        The same consensus math the live fair values use, so closing lines
        cannot drift apart from what the model priced live.
        """
        ...

    @property
    def devig_method(self) -> str:
        """Vig-removal method ("power", "multiplicative", "additive")."""
        ...

    @property
    def max_quote_age_s(self) -> float:
        """Quotes confirmed longer ago than this are never used."""
        ...

    @property
    def nfl_tie_prob(self) -> float:
        """ADR-0011: P(an NFL game ties), for the tie-refund conversion."""
        ...

    @property
    def nfl_tie_prob_se(self) -> float:
        """ADR-0011: uncertainty on the tie rate, added in quadrature."""
        ...


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
    # ADR-0008 alert-first paper tracking
    def record_call(self, call: Call) -> None: ...
    def record_call_snapshot(self, snap: CallSnapshot) -> None: ...
    def record_paper_fill(self, fill: PaperFill) -> None: ...
    # ADR-0008 part 2: grading
    def record_manual_fill(self, fill: ManualFill) -> None: ...
    def record_closing_line(self, line: ClosingLine) -> None: ...
    def record_settlement(self, settlement: Settlement) -> None: ...
    def record_call_grade(self, grade: CallGrade) -> None: ...
    # Closing line capture: query historical quotes.
    # Each entry: (venue, price, valid_at, recorded_at). recorded_at is the
    # confirmation time; staleness gates on it per ADR-0007.
    def latest_quotes_before(
        self, outcome_key: str, before: datetime
    ) -> list[tuple[str, float, str, str]]: ...
    def has_closing_line(self, outcome_key: str) -> bool: ...
    def canonical_outcome_key(self, outcome_key: str) -> str: ...
    def record_key_remap(self, old_key: str, new_key: str, reason: str, at: datetime) -> None: ...
    # Settlement: get calls awaiting results
    def unsettled_calls(self) -> list[tuple[str, str, str, str]]: ...
    def settled_calls(self) -> list[dict[str, Any]]: ...
    def all_calls(self) -> list[dict[str, Any]]: ...
    # Grading: fetch call data for grading
    def get_call(self, call_id: str) -> dict[str, Any] | None: ...
    def get_paper_fill(self, call_id: str) -> dict[str, Any] | None: ...
    def get_manual_fill(self, call_id: str) -> dict[str, Any] | None: ...
    def get_snapshots(self, call_id: str) -> list[dict[str, Any]]: ...
    def get_closing_line(self, outcome_key: str) -> dict[str, Any] | None: ...
    def get_closing_line_version(
        self, outcome_key: str, method_version: int
    ) -> dict[str, Any] | None: ...
    def get_call_grade(self, call_id: str) -> dict[str, Any] | None: ...
    def get_call_grade_version(
        self, call_id: str, method_version: int
    ) -> dict[str, Any] | None: ...


class ExecutionVenue(Protocol):
    name: str

    async def buy(self, opportunity: Opportunity) -> Fill | None: ...


class Notifier(Protocol):
    async def send(self, opportunity: Opportunity) -> bool: ...
