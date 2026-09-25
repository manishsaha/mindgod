"""Tests for ADR-0008 part 1: calls, paper fills, snapshots."""

from datetime import UTC, datetime
from decimal import Decimal

from mindgod.adapters.config import ListingSpec
from mindgod.adapters.listings import build_listing
from mindgod.application.calls import (
    build_call,
    instant_fill,
    reaction_fill,
    snapshot_for,
)
from mindgod.application.opportunities import DetectorConfig, Opportunity
from mindgod.domain.fees import QuadraticFeeModel
from mindgod.domain.primitives import ContractPrice, Probability
from mindgod.domain.quotes import OrderBook, PriceLevel
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import ListingKey


def _listing_key():
    return ListingKey(venue_id="kalshi", market_id="KXNFLGAME-1", side="yes")


def _outcome():
    spec = ListingSpec(
        venue="kalshi",
        market_id="KXNFLGAME-1",
        side="yes",
        league="nfl",
        home="KC",
        away="BUF",
        start="2026-10-05T17:00:00Z",
        outcome_kind="spread",
        outcome_team="home",
        handicap="-3",
    )
    listing = build_listing(spec)
    return listing.outcome


def _fair():
    return FairValue(
        outcome=_outcome(),
        probability=Probability(Decimal("0.60")),
        standard_error=0.02,
        as_of=datetime.now(UTC),
        method="test",
        sources=(),
    )


def _book():
    from mindgod.domain.primitives import Observation

    now = datetime.now(UTC)
    return OrderBook(
        listing=_listing_key(),
        asks=(
            PriceLevel(ContractPrice(Decimal("0.50")), 10),
            PriceLevel(ContractPrice(Decimal("0.55")), 10),
        ),
        bids=(PriceLevel(ContractPrice(Decimal("0.48")), 10),),
        observed=Observation(now, now),
    )


def _opp():
    from mindgod.domain.quotes import FillEstimate

    return Opportunity(
        listing=_listing_key(),
        outcome=_outcome(),
        fair_value=_fair(),
        fill=FillEstimate(
            contracts=10,
            notional=Decimal("5.20"),
            worst_price=ContractPrice(Decimal("0.55")),
        ),
        fee=Decimal("0.50"),
        edge_net=Decimal("0.05"),
        stake=Decimal("50"),
        limit_price=Decimal("0.55"),
        depth_at_x=20,
    )


def _fees():
    return QuadraticFeeModel(Decimal("0.07"), Decimal("0"))


def _cfg():
    return DetectorConfig(
        bankroll=Decimal("10000"),
        min_net_edge=Decimal("0.02"),
        kelly_fraction=Decimal("0.25"),
        max_stake_per_bet=Decimal("100"),
        slippage=Decimal("0.005"),
        uncertainty_aversion=Decimal("10"),
        threshold_widening=Decimal("2"),
    )


def test_build_call_none_when_no_limit():
    from mindgod.domain.quotes import FillEstimate

    opp = Opportunity(
        listing=_listing_key(),
        outcome=_outcome(),
        fair_value=_fair(),
        fill=FillEstimate(
            contracts=10,
            notional=Decimal("5.20"),
            worst_price=ContractPrice(Decimal("0.55")),
        ),
        fee=Decimal("0.50"),
        edge_net=Decimal("0.05"),
        stake=Decimal("50"),
        limit_price=None,
        depth_at_x=0,
    )
    assert build_call(opp, _book(), None, datetime.now(UTC)) is None


def test_instant_fill_fills_up_to_x():
    now = datetime.now(UTC)
    call = build_call(_opp(), _book(), None, now)
    assert call is not None
    fill = instant_fill(call, _book(), _fees(), _cfg(), _fair(), now)
    assert fill.filled
    # Book has 20 contracts at or below X=0.55, call wants up to 20
    assert fill.contracts <= 20
    assert fill.kind == "instant"


def test_reaction_fill_no_fill_when_edge_gone():
    from mindgod.domain.primitives import Observation

    now = datetime.now(UTC)
    call = build_call(_opp(), _book(), None, now)
    assert call is not None
    # Book moved up: asks now at 0.65, above X=0.55
    moved = OrderBook(
        listing=_listing_key(),
        asks=(PriceLevel(ContractPrice(Decimal("0.65")), 10),),
        bids=(),
        observed=Observation(now, now),
    )
    fill = reaction_fill(call, moved, _fees(), _cfg(), _fair(), now)
    assert not fill.filled
    assert fill.contracts == 0


def test_snapshot_records_book_state():
    now = datetime.now(UTC)
    call = build_call(_opp(), _book(), None, now)
    assert call is not None
    snap = snapshot_for(call, _book(), 30, now)
    assert snap.offset_s == 30
    assert snap.best_ask == Decimal("0.50")
    assert snap.best_bid == Decimal("0.48")
    assert snap.depth_at_x == 20
