"""ADR-0008 alert-first: calls, paper fills, and decay snapshots.

The alert is the product. Each call records a price limit X; paper fills model
human reaction time by walking the book observed reaction_delay_s after the
alert up to X. If the edge is gone, it is recorded as "no fill," which is
itself a result. Decay snapshots at +0/+30s/+2min/+10min give each call a
decay curve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from mindgod.domain.fees import FeeModel
from mindgod.domain.primitives import ContractPrice
from mindgod.domain.propositions import Outcome
from mindgod.domain.quotes import OrderBook
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import ListingKey

from .opportunities import DetectorConfig, Opportunity


@dataclass(frozen=True, slots=True)
class Call:
    """A single alert: buy up to N contracts at ≤ X."""

    call_id: str
    created_at: datetime
    listing_key: ListingKey
    outcome: Outcome
    fair_prob: float
    fair_se: float
    fair_method: str
    limit_price_x: Decimal
    contracts_n: int
    ask_at_alert: Decimal
    net_edge_at_alert: Decimal
    depth_at_x: int
    event_start: datetime | None


@dataclass(frozen=True, slots=True)
class PaperFill:
    call_id: str
    kind: str  # "reaction" | "instant"
    contracts: int
    avg_price: Decimal | None
    fee: Decimal | None
    filled: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class CallSnapshot:
    call_id: str
    offset_s: int  # 0, 30, 120, 600
    best_ask: Decimal | None
    best_bid: Decimal | None
    depth_at_x: int
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ManualFill:
    """A fill the user logged by hand via the Discord 'Took it' button."""

    call_id: str
    contracts: int
    price: Decimal
    fee: Decimal
    taken_at: datetime
    note: str = ""


# Method versions for the append-only recompute (ADR-0008 grading loop).
# Version 1: rows written before the af2b1b7 review round (vig-included
# closes, or devigged closes gated on valid_at instead of recorded_at, and
# MLB moneylines that could not pair). Version 2: the current method
# (devigged complement-pair closes gated on confirmation age, with the MLB
# full-game no-tie normalization). Old rows are never overwritten or
# deleted; reports read only the latest version. Bump when the method
# changes and recompute.
CLOSING_LINE_METHOD_VERSION = 2
CALL_GRADE_METHOD_VERSION = 2


@dataclass(frozen=True, slots=True)
class ClosingLine:
    """Last sharp consensus before the event locks."""

    outcome_key: str
    sharp_close_prob: float
    source: str
    captured_at: datetime
    method_version: int = CLOSING_LINE_METHOD_VERSION


@dataclass(frozen=True, slots=True)
class Settlement:
    outcome_key: str
    result: str  # "win" | "loss" | "refund" | "void"
    settled_at: datetime


@dataclass(frozen=True, slots=True)
class CallGrade:
    """CLV and P&L for a call, computed after close and updated after settlement."""

    call_id: str
    clv_reaction: float | None  # sharp_close - fill_avg - fee_per_contract
    clv_manual: float | None
    pnl_reaction: float | None  # per-contract P&L after settlement
    pnl_manual: float | None
    edge_half_life_s: float | None  # seconds until ask crosses halfway to X
    graded_at: datetime
    method_version: int = CALL_GRADE_METHOD_VERSION


def build_call(
    opp: Opportunity,
    book: OrderBook,
    event_start: datetime | None,
    at: datetime,
) -> Call | None:
    """Build a Call from an Opportunity and its alert-time book."""
    if opp.limit_price is None:
        return None
    ask = book.asks[0].price.dollars if book.asks else opp.fill.average_price
    return Call(
        call_id=f"call_{uuid.uuid4().hex[:12]}",
        created_at=at,
        listing_key=opp.listing,
        outcome=opp.outcome,
        fair_prob=float(opp.fair_value.probability.value),
        fair_se=float(opp.fair_value.standard_error),
        fair_method=opp.fair_value.method,
        limit_price_x=opp.limit_price,
        contracts_n=opp.fill.contracts,
        ask_at_alert=ask,
        net_edge_at_alert=opp.edge_net,
        depth_at_x=opp.depth_at_x,
        event_start=event_start,
    )


def _walk_up_to_x(
    book: OrderBook,
    limit_x: Decimal,
    want: int,
    fees: FeeModel,
    cfg: DetectorConfig,
    fair: FairValue,
) -> tuple[int, Decimal | None, Decimal | None, bool]:
    """Walk the ask ladder up to X. Returns (contracts, avg_price, fee, filled).

    filled=False when the book has no depth at or below X (edge gone).
    """
    remaining = want
    notional = Decimal(0)
    taken = 0
    for level in book.asks:
        if level.price.dollars > limit_x:
            break
        take = min(remaining, level.contracts)
        notional += take * level.price.dollars
        taken += take
        remaining -= take
        if remaining == 0:
            break
    if taken == 0:
        return 0, None, None, False
    avg = notional / taken
    # Re-check edge at the reaction book: if the average price no longer
    # clears the threshold, it is a "no fill" (edge disappeared).
    fair_p = Decimal(str(fair.probability.value))
    se = Decimal(str(fair.standard_error))
    threshold = cfg.min_net_edge + cfg.threshold_widening * se
    fee = fees.taker_fee(ContractPrice(avg), taken)
    edge = fair_p - avg - fee / taken - cfg.slippage
    if edge < threshold:
        return 0, None, None, False
    return taken, avg, fee, True


def instant_fill(
    call: Call,
    book: OrderBook,
    fees: FeeModel,
    cfg: DetectorConfig,
    fair: FairValue,
    at: datetime,
) -> PaperFill:
    """Reference fill at alert-time price (never feeds headline metrics)."""
    contracts, avg, fee, filled = _walk_up_to_x(
        book, call.limit_price_x, call.contracts_n, fees, cfg, fair
    )
    return PaperFill(
        call_id=call.call_id,
        kind="instant",
        contracts=contracts,
        avg_price=avg,
        fee=fee,
        filled=filled,
        at=at,
    )


def reaction_fill(
    call: Call,
    book: OrderBook,
    fees: FeeModel,
    cfg: DetectorConfig,
    fair: FairValue,
    at: datetime,
) -> PaperFill:
    """Paper fill against the book observed reaction_delay_s after the alert."""
    contracts, avg, fee, filled = _walk_up_to_x(
        book, call.limit_price_x, call.contracts_n, fees, cfg, fair
    )
    return PaperFill(
        call_id=call.call_id,
        kind="reaction",
        contracts=contracts,
        avg_price=avg,
        fee=fee,
        filled=filled,
        at=at,
    )


def snapshot_for(
    call: Call,
    book: OrderBook,
    offset_s: int,
    at: datetime,
) -> CallSnapshot:
    """Decay snapshot: book state at a fixed offset after the alert."""
    best_ask = book.asks[0].price.dollars if book.asks else None
    best_bid = book.bids[0].price.dollars if book.bids else None
    depth = sum(lvl.contracts for lvl in book.asks if lvl.price.dollars <= call.limit_price_x)
    return CallSnapshot(
        call_id=call.call_id,
        offset_s=offset_s,
        best_ask=best_ask,
        best_bid=best_bid,
        depth_at_x=depth,
        recorded_at=at,
    )


def edge_half_life_s(call: Call, snapshots: list[CallSnapshot]) -> float | None:
    """Estimate seconds until the ask crosses halfway from alert ask to X.

    Uses the decay snapshots. Returns None when the snapshots never show the
    ask reaching the halfway point.
    """
    halfway = (call.ask_at_alert + call.limit_price_x) / 2
    # Snapshots are ordered by offset; find the first at or past halfway.
    for snap in sorted(snapshots, key=lambda s: s.offset_s):
        if snap.best_ask is not None and snap.best_ask >= halfway:
            return float(snap.offset_s)
    return None


def clv_for_fill(
    fill_avg_price: Decimal | None,
    fee_per_contract: Decimal | None,
    sharp_close_prob: float,
    filled: bool,
) -> float | None:
    """Closing line value per contract, in probability points.

    clv = sharp_close_prob - fill_avg_price - fee_per_contract.
    Positive means we beat the close.
    """
    if not filled or fill_avg_price is None:
        return None
    fee = float(fee_per_contract) if fee_per_contract is not None else 0.0
    return sharp_close_prob - float(fill_avg_price) - fee


def pnl_for_fill(
    fill_avg_price: Decimal | None,
    fee_per_contract: Decimal | None,
    result: str,
    filled: bool,
) -> float | None:
    """Per-contract P&L after settlement.

    Win pays (1 - price), loss pays (-price), refund/void pays 0, all net of fee.
    """
    if not filled or fill_avg_price is None:
        return None
    price = float(fill_avg_price)
    fee = float(fee_per_contract) if fee_per_contract is not None else 0.0
    if result == "win":
        return (1.0 - price) - fee
    if result == "loss":
        return -price - fee
    # refund / void: stake returned, only fee lost
    return -fee


def grade_call(
    call: Call,
    reaction_fill: PaperFill | None,
    manual_fill: ManualFill | None,
    snapshots: list[CallSnapshot],
    closing_line: ClosingLine | None,
    settlement: Settlement | None,
    at: datetime,
) -> CallGrade:
    """Grade a call: CLV after close, P&L after settlement, edge half-life."""
    clv_r = None
    clv_m = None
    if closing_line is not None:
        if reaction_fill is not None:
            fee_pc = (
                reaction_fill.fee / reaction_fill.contracts
                if reaction_fill.fee is not None and reaction_fill.contracts > 0
                else None
            )
            clv_r = clv_for_fill(
                reaction_fill.avg_price,
                fee_pc,
                closing_line.sharp_close_prob,
                reaction_fill.filled,
            )
        if manual_fill is not None:
            fee_pc_m = (
                manual_fill.fee / manual_fill.contracts if manual_fill.contracts > 0 else None
            )
            clv_m = clv_for_fill(
                manual_fill.price,
                fee_pc_m,
                closing_line.sharp_close_prob,
                True,
            )
    pnl_r = None
    pnl_m = None
    if settlement is not None:
        if reaction_fill is not None:
            fee_pc = (
                reaction_fill.fee / reaction_fill.contracts
                if reaction_fill.fee is not None and reaction_fill.contracts > 0
                else None
            )
            pnl_r = pnl_for_fill(
                reaction_fill.avg_price,
                fee_pc,
                settlement.result,
                reaction_fill.filled,
            )
        if manual_fill is not None:
            fee_pc_m = (
                manual_fill.fee / manual_fill.contracts if manual_fill.contracts > 0 else None
            )
            pnl_m = pnl_for_fill(
                manual_fill.price,
                fee_pc_m,
                settlement.result,
                True,
            )
    return CallGrade(
        call_id=call.call_id,
        clv_reaction=clv_r,
        clv_manual=clv_m,
        pnl_reaction=pnl_r,
        pnl_manual=pnl_m,
        edge_half_life_s=edge_half_life_s(call, snapshots),
        graded_at=at,
    )
