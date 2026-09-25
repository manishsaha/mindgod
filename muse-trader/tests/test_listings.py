"""Listing registry tests: explicit registration, terms-change quarantine."""

from datetime import UTC, datetime

import pytest

from mindgod.adapters.config import ListingSpec
from mindgod.adapters.listings import RegistryResolver, build_event, build_listing
from mindgod.application.ports import UnmappedMarket
from mindgod.domain.propositions import moneyline
from mindgod.domain.sports import TeamId
from mindgod.domain.venues import VenueId


def _spec(**over) -> ListingSpec:
    kw: dict = dict(
        venue="kalshi",
        market_id="T1",
        side="yes",
        league="nfl",
        home="KC",
        away="BUF",
        start="2026-10-05T17:00:00Z",
        outcome_kind="moneyline",
        outcome_team="home",
    )
    kw.update(over)
    return ListingSpec(**kw)


def test_builds_canonical_outcome():
    spec = _spec()
    listing = build_listing(spec)
    event = build_event(spec)
    assert listing.outcome == moneyline(event, TeamId("nfl-kc"))
    assert listing.key.venue_id == VenueId("kalshi")


def test_event_identity_uses_date_and_game_number():
    one = build_event(_spec(start="2026-10-05T17:00:00Z", game_number=1))
    moved = build_event(_spec(start="2026-10-05T21:25:00Z", game_number=1))
    second = build_event(_spec(start="2026-10-05T17:00:00Z", game_number=2))
    assert one.id == moved.id  # flexed kickoff does not change identity
    assert one.id != second.id  # doubleheader games stay distinct
    assert "2026-10-05" in str(one.id)


def test_no_side_complements_the_outcome():
    spec = _spec(side="no")
    listing = build_listing(spec)
    event = build_event(spec)
    assert listing.outcome == moneyline(event, TeamId("nfl-kc")).complement()


def test_build_event_requires_a_parseable_start():
    with pytest.raises(ValueError):
        build_event(_spec(start=""))
    with pytest.raises(ValueError):
        build_event(_spec(start="not-a-date"))


def test_note_event_remembers_starts():
    resolver = RegistryResolver()
    listing = build_listing(_spec())
    event = build_event(_spec())
    resolver.register(listing)
    resolver.note_event(listing.key, event)
    assert resolver.event_start(listing.key) == event.scheduled_start
    spec = _spec(side="no")
    listing = build_listing(spec)
    event = build_event(spec)
    assert listing.outcome == moneyline(event, TeamId("nfl-kc")).complement()


def test_resolve_roundtrip():
    resolver = RegistryResolver()
    listing = build_listing(_spec())
    resolver.register(listing)
    assert resolver.resolve(listing.key) == listing
    assert resolver.listings_for(VenueId("kalshi")) == [listing]


def test_terms_change_goes_to_review_not_overwrite():
    resolver = RegistryResolver()
    original = build_listing(_spec())
    resolver.register(original)
    changed = build_listing(_spec(refunds_on_tie=True))
    resolver.register(changed)
    assert len(resolver.review_queue) == 1
    assert resolver.resolve(original.key) == original  # old terms kept


def test_report_unmapped_dedups():
    resolver = RegistryResolver()
    market = UnmappedMarket(
        VenueId("kalshi"),
        "T9",
        "Some Game",
        datetime(2026, 10, 5, tzinfo=UTC),
        "unregistered ticker",
    )
    resolver.report_unmapped(market)
    resolver.report_unmapped(market)
    assert len(resolver.review_queue) == 1
