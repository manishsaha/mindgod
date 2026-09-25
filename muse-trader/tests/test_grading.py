"""Tests for ADR-0008 part 2: grading, CLV, P&L, half-life."""

from datetime import UTC, datetime
from decimal import Decimal

from mindgod.application.calls import (
    Call,
    CallSnapshot,
    ClosingLine,
    ManualFill,
    PaperFill,
    Settlement,
    clv_for_fill,
    edge_half_life_s,
    grade_call,
    pnl_for_fill,
)
from mindgod.domain.venues import ListingKey


def _call():
    return Call(
        call_id="call_test123",
        created_at=datetime.now(UTC),
        listing_key=ListingKey(venue_id="kalshi", market_id="M1", side="yes"),
        outcome=None,  # type: ignore[arg-type]
        fair_prob=0.60,
        fair_se=0.02,
        fair_method="test",
        limit_price_x=Decimal("0.55"),
        contracts_n=10,
        ask_at_alert=Decimal("0.50"),
        net_edge_at_alert=Decimal("0.05"),
        depth_at_x=20,
        event_start=None,
    )


def test_clv_positive_when_beating_close():
    # Bought at 0.50, close at 0.55, fee 0.01: CLV = 0.55 - 0.50 - 0.01 = 0.04
    clv = clv_for_fill(Decimal("0.50"), Decimal("0.01"), 0.55, True)
    assert clv is not None
    assert abs(clv - 0.04) < 1e-9


def test_clv_none_when_no_fill():
    assert clv_for_fill(None, None, 0.55, False) is None
    assert clv_for_fill(Decimal("0.50"), Decimal("0.01"), 0.55, False) is None


def test_pnl_win():
    # Bought at 0.50, won, fee 0.01: P&L = (1 - 0.50) - 0.01 = 0.49
    pnl = pnl_for_fill(Decimal("0.50"), Decimal("0.01"), "win", True)
    assert pnl is not None
    assert abs(pnl - 0.49) < 1e-9


def test_pnl_loss():
    # Bought at 0.50, lost, fee 0.01: P&L = -0.50 - 0.01 = -0.51
    pnl = pnl_for_fill(Decimal("0.50"), Decimal("0.01"), "loss", True)
    assert pnl is not None
    assert abs(pnl - (-0.51)) < 1e-9


def test_pnl_refund():
    # Refund: only fee lost
    pnl = pnl_for_fill(Decimal("0.50"), Decimal("0.01"), "refund", True)
    assert pnl is not None
    assert abs(pnl - (-0.01)) < 1e-9


def test_edge_half_life():
    call = _call()
    # Ask at alert 0.50, X 0.55, halfway 0.525
    # Snapshots: +0 at 0.50, +30 at 0.52, +120 at 0.53 (crosses 0.525)
    now = datetime.now(UTC)
    snaps = [
        CallSnapshot("call_test123", 0, Decimal("0.50"), None, 20, now),
        CallSnapshot("call_test123", 30, Decimal("0.52"), None, 15, now),
        CallSnapshot("call_test123", 120, Decimal("0.53"), None, 10, now),
    ]
    assert edge_half_life_s(call, snaps) == 120.0


def test_edge_half_life_none_when_never_crosses():
    call = _call()
    now = datetime.now(UTC)
    snaps = [
        CallSnapshot("call_test123", 0, Decimal("0.50"), None, 20, now),
        CallSnapshot("call_test123", 30, Decimal("0.51"), None, 15, now),
    ]
    assert edge_half_life_s(call, snaps) is None


def test_grade_call_full():
    call = _call()
    now = datetime.now(UTC)
    reaction = PaperFill(
        call_id="call_test123",
        kind="reaction",
        contracts=10,
        avg_price=Decimal("0.52"),
        fee=Decimal("0.50"),
        filled=True,
        at=now,
    )
    manual = ManualFill(
        call_id="call_test123",
        contracts=5,
        price=Decimal("0.51"),
        fee=Decimal("0.25"),
        taken_at=now,
        note="",
    )
    snaps = [
        CallSnapshot("call_test123", 0, Decimal("0.50"), None, 20, now),
        CallSnapshot("call_test123", 30, Decimal("0.53"), None, 15, now),
    ]
    closing = ClosingLine(
        outcome_key="test",
        sharp_close_prob=0.58,
        source="pinnacle",
        captured_at=now,
    )
    settlement = Settlement(
        outcome_key="test",
        result="win",
        settled_at=now,
    )
    grade = grade_call(call, reaction, manual, snaps, closing, settlement, now)
    # CLV reaction: 0.58 - 0.52 - 0.05 = 0.01
    assert grade.clv_reaction is not None
    assert abs(grade.clv_reaction - 0.01) < 1e-9
    # CLV manual: 0.58 - 0.51 - 0.05 = 0.02
    assert grade.clv_manual is not None
    assert abs(grade.clv_manual - 0.02) < 1e-9
    # P&L reaction: (1 - 0.52) - 0.05 = 0.43
    assert grade.pnl_reaction is not None
    assert abs(grade.pnl_reaction - 0.43) < 1e-9
    # Half-life: halfway 0.525, crossed at +30
    assert grade.edge_half_life_s == 30.0
