"""Listing registry tests: explicit registration, terms-change quarantine."""
from datetime import UTC, datetime

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


def test_no_side_complements_the_outcome():
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
        VenueId("kalshi"), "T9", "Some Game", datetime(2026, 10, 5, tzinfo=UTC),
        "unregistered ticker",
    )
    resolver.report_unmapped(market)
    resolver.report_unmapped(market)
    assert len(resolver.review_queue) == 1
