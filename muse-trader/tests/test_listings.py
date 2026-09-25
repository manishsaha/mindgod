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


def test_underdog_spread_push_matches_favorite():
    """build_terms on the underdog side: BUF +3 refunds exactly when KC
    wins by 3, the same refund outcome the favorite's -3 carries."""
    from decimal import Decimal

    from mindgod.adapters.listings import build_outcome, build_terms
    from mindgod.domain.propositions import margin_exactly

    fav_spec = _spec(outcome_kind="spread", outcome_team="home", handicap="-3")
    dog_spec = _spec(outcome_kind="spread", outcome_team="away", handicap="3")
    event = build_event(fav_spec)
    fav_terms = build_terms(fav_spec, event, build_outcome(fav_spec, event))
    dog_terms = build_terms(dog_spec, event, build_outcome(dog_spec, event))
    push = margin_exactly(event, TeamId("nfl-kc"), Decimal("3"))
    assert fav_terms.payoff.refunds_if == push
    assert dog_terms.payoff.refunds_if == push
