"""Listing registry: turn configured market specs into canonical listings.

Adapters build outcomes with the builders in domain.propositions, so each
real-world outcome has exactly one representation. Re-registering a listing
whose terms changed is refused and routed to the review queue: a changed bet
wearing the same ticker is the settlement-mismatch trap from the principles.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from mindgod.application.ports import ListingResolver, UnmappedMarket
from mindgod.domain.propositions import (
    Comparator,
    Outcome,
    margin_exactly,
    moneyline,
    spread,
    total,
)
from mindgod.domain.sports import Event, EventId, League, TeamId
from mindgod.domain.terms import Payoff, Terms, differences
from mindgod.domain.venues import Listing, ListingKey, VenueId

from .config import ListingSpec


def event_id_for(
    league: League, home_abbr: str, away_abbr: str, scheduled: datetime
) -> EventId:
    """Shared event identity so the odds feed and the listing seed build the
    same canonical outcomes. Minute precision disambiguates doubleheaders."""
    stamp = scheduled.strftime("%Y%m%dT%H%M")
    return EventId(
        f"{league.value}-{away_abbr.lower()}-at-{home_abbr.lower()}-{stamp}"
    )


def build_event(spec: ListingSpec) -> Event:
    league = League(spec.league)
    home = TeamId(f"{league.value}-{spec.home.lower()}")
    away = TeamId(f"{league.value}-{spec.away.lower()}")
    try:
        scheduled = datetime.fromisoformat(spec.start.replace("Z", "+00:00"))
    except ValueError:
        scheduled = datetime(2999, 1, 1, tzinfo=UTC)
    return Event(
        id=event_id_for(league, spec.home, spec.away, scheduled),
        league=league,
        home=home,
        away=away,
        scheduled_start=scheduled,
        game_number=spec.game_number,
    )


def build_outcome(spec: ListingSpec, event: Event) -> Outcome:
    team = event.home if spec.outcome_team == "home" else event.away
    if spec.outcome_kind == "moneyline":
        outcome = moneyline(event, team)
    elif spec.outcome_kind == "spread":
        outcome = spread(event, team, Decimal(spec.handicap))
    elif spec.outcome_kind == "total":
        comparator = (
            Comparator.GT if spec.total_side == "over" else Comparator.LT
        )
        outcome = total(event, comparator, Decimal(spec.total_line))
    else:
        raise ValueError(f"unknown outcome kind: {spec.outcome_kind}")
    if spec.side == "no":
        complemented = outcome.complement()
        if complemented is None:
            raise ValueError(f"cannot take the no side of {spec.outcome_kind}")
        return complemented
    if spec.side != "yes":
        raise ValueError(f"unknown side: {spec.side}")
    return outcome


def build_terms(spec: ListingSpec, event: Event, outcome: Outcome) -> Terms:
    refunds = None
    if spec.refunds_on_tie and spec.outcome_kind == "moneyline":
        team = event.home if spec.outcome_team == "home" else event.away
        refunds = margin_exactly(event, team, Decimal(0))
    return Terms(payoff=Payoff(wins_if=outcome, refunds_if=refunds))


def build_listing(spec: ListingSpec) -> Listing:
    event = build_event(spec)
    outcome = build_outcome(spec, event)
    return Listing(
        key=ListingKey(
            venue_id=VenueId(spec.venue),
            market_id=spec.market_id,
            side=spec.side,
        ),
        terms=build_terms(spec, event, outcome),
    )


class RegistryResolver(ListingResolver):
    """Explicit registration is the source of truth. Nothing is guessed."""

    def __init__(self) -> None:
        self._table: dict[ListingKey, Listing] = {}
        self._review: list[UnmappedMarket] = []

    def register(self, listing: Listing) -> None:
        existing = self._table.get(listing.key)
        if existing is not None and differences(existing.terms, listing.terms):
            self._review.append(
                UnmappedMarket(
                    venue_id=listing.key.venue_id,
                    market_id=listing.key.market_id,
                    label=listing.key.side,
                    seen_at=datetime.now(UTC),
                    reason="terms changed under a registered listing",
                )
            )
            return
        self._table[listing.key] = listing

    def resolve(self, key: ListingKey) -> Listing | None:
        return self._table.get(key)

    def listings_for(self, venue_id: VenueId) -> list[Listing]:
        return [listing for k, listing in self._table.items() if k.venue_id == venue_id]

    def report_unmapped(self, market: UnmappedMarket) -> None:
        if not any(
            m.venue_id == market.venue_id and m.market_id == market.market_id
            for m in self._review
        ):
            self._review.append(market)

    @property
    def review_queue(self) -> list[UnmappedMarket]:
        return list(self._review)
