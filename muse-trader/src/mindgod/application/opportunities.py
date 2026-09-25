"""Opportunity detection: fee-aware net-edge gating with fractional Kelly.

The gate is applied to NET edge (gross edge minus per-contract taker fee
minus slippage), never to gross edge. A flat threshold on gross edge is
wrong because the taker fee is price-dependent: it peaks at 50c and shrinks
toward the tails. Rounding up per order also means tiny orders pay a much
higher effective rate; the net-edge gate kills those automatically.

Sizing is two-pass fractional Kelly: size tentatively on gross edge to learn
the order's contract count (which sets the per-contract fee under round-up),
gate on net edge, then size finally on net edge. Uncertainty widens the gate
and shrinks the stake. Depth caps the size: we price the whole ladder, so the
final edge is computed at the fill's average price, not the top of the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from mindgod.domain.fees import FeeModel
from mindgod.domain.primitives import ContractPrice, Probability
from mindgod.domain.propositions import Outcome
from mindgod.domain.quotes import FillEstimate, OrderBook
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing, ListingKey, VenueId


@dataclass(frozen=True, slots=True)
class Opportunity:
    listing: ListingKey
    outcome: Outcome
    fair_value: FairValue
    fill: FillEstimate
    fee: Decimal
    edge_net: Decimal  # per-contract expected profit, net of fee and slippage
    stake: Decimal
    refs: str = ""
    limit_price: Decimal | None = None  # X: highest ask with edge, ADR-0008
    depth_at_x: int = 0  # contracts available at or below X


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    bankroll: Decimal
    min_net_edge: Decimal = Decimal("0.02")
    kelly_fraction: Decimal = Decimal("0.25")
    max_stake_per_bet: Decimal = Decimal("100")
    slippage: Decimal = Decimal("0.005")
    uncertainty_aversion: Decimal = Decimal("10")  # stake shrink per unit of se
    threshold_widening: Decimal = Decimal("2")  # edge-gate widening per unit of se


def _kelly_stake(
    edge: Decimal,
    price: Decimal,
    bankroll: Decimal,
    kelly_mult: Decimal,
    max_stake: Decimal,
) -> Decimal:
    """Full-Kelly stake for a $1-payout contract bought at `price`.

    f* = edge / (1 - price). Returns 0 when there is no edge.
    """
    if price <= 0 or price >= 1 or edge <= 0:
        return Decimal(0)
    return min(edge / (1 - price) * kelly_mult * bankroll, max_stake)


def compute_limit_price(
    fair: FairValue,
    fees: FeeModel,
    cfg: DetectorConfig,
    contracts: int,
    current_price: Decimal,
) -> Decimal:
    """Highest ask price X where net edge still clears the threshold.

    ADR-0008: the alert states a price limit, not a price. X is computed with
    the existing fee model, so the user can act without redoing math.
    """
    fair_p = Decimal(str(fair.probability.value))
    se = Decimal(str(fair.standard_error))
    threshold = cfg.min_net_edge + cfg.threshold_widening * se

    def net_edge_at(p: Decimal) -> Decimal:
        if p <= 0 or p >= 1:
            return Decimal("-1")
        fee_per = fees.taker_fee(ContractPrice(p), contracts) / contracts
        return fair_p - p - fee_per - cfg.slippage

    lo = current_price
    hi = fair_p - threshold - cfg.slippage
    if hi >= Decimal("1"):
        hi = Decimal("0.99")
    if hi <= lo:
        return lo
    # Binary search for max p with net_edge >= threshold
    for _ in range(25):
        mid = (lo + hi) / 2
        if net_edge_at(mid) >= threshold:
            lo = mid
        else:
            hi = mid
    # Round down to the cent: X is a limit the user can actually hit
    cents = (lo * 100).to_integral_value(rounding=ROUND_FLOOR)
    return cents / 100


def evaluate(
    listing: Listing,
    book: OrderBook,
    fair: FairValue,
    fees: FeeModel,
    cfg: DetectorConfig,
    refs: str = "",
) -> Opportunity | None:
    """Build an Opportunity if the NET edge clears the threshold, else None."""
    if not book.asks:
        return None
    price = book.asks[0].price.dollars
    fair_p = Decimal(str(fair.probability.value))
    se = Decimal(str(fair.standard_error))

    gross_edge = fair_p - price
    if gross_edge <= 0:
        return None

    # Pass 1: tentative size on gross edge -> contract count -> per-contract fee.
    tentative = _kelly_stake(
        gross_edge, price, cfg.bankroll, cfg.kelly_fraction, cfg.max_stake_per_bet
    )
    contracts_1 = int(tentative / price)
    if contracts_1 < 1:
        return None
    fee_1 = fees.taker_fee(ContractPrice(price), contracts_1)
    fee_per_contract = fee_1 / contracts_1

    # The gate: net of fee and slippage, widened by uncertainty.
    threshold = cfg.min_net_edge + cfg.threshold_widening * se
    net_edge = gross_edge - fee_per_contract - cfg.slippage
    if net_edge < threshold:
        return None

    # Pass 2: final sizing on net edge, shrunk by uncertainty, capped by depth.
    shrink = Decimal(1) / (Decimal(1) + cfg.uncertainty_aversion * se)
    stake = _kelly_stake(
        net_edge,
        price,
        cfg.bankroll,
        cfg.kelly_fraction * shrink,
        cfg.max_stake_per_bet,
    )
    depth = sum(level.contracts for level in book.asks)
    contracts = min(int(stake / price), depth)
    if contracts < 1:
        return None
    fill = book.cost_to_buy(contracts)
    if fill is None:  # unreachable after the depth cap; defensive
        return None
    if Decimal(contracts) * fill.average_price > cfg.max_stake_per_bet:
        # walking the ladder pushed the cost over the cap; trim to fit
        contracts = int(cfg.max_stake_per_bet / fill.average_price)
        if contracts < 1:
            return None
        fill = book.cost_to_buy(contracts)
        if fill is None:
            return None
    fee = fees.taker_fee(ContractPrice(fill.average_price), contracts)
    edge_net = fair_p - fill.average_price - fee / contracts - cfg.slippage
    if edge_net < threshold:
        return None
    # ADR-0008: X is the highest ask where edge still clears the threshold.
    # N is Kelly-sized contracts capped by depth at or below X.
    limit_x = compute_limit_price(fair, fees, cfg, contracts, fill.average_price)
    depth_at_x = sum(level.contracts for level in book.asks if level.price.dollars <= limit_x)
    return Opportunity(
        listing=listing.key,
        outcome=listing.outcome,
        fair_value=fair,
        fill=fill,
        fee=fee,
        edge_net=edge_net,
        stake=Decimal(contracts) * fill.average_price,
        refs=refs,
        limit_price=limit_x,
        depth_at_x=depth_at_x,
    )


def _with_complements(
    fair_values: dict[Outcome, FairValue],
) -> dict[Outcome, FairValue]:
    """Index fair values by complement too.

    A listing on "No" is the complement outcome; if books priced the "Yes"
    side, 1 - p is its fair value. Direct prices win over complements. Note
    this is exact where a naive 1 - p flip was not: the complement of a
    moneyline includes the tie, which a 2-way book refunds on.
    """
    extended = dict(fair_values)
    for outcome, fv in fair_values.items():
        comp = outcome.complement()
        if comp is not None and comp not in extended:
            extended[comp] = FairValue(
                outcome=comp,
                probability=Probability(1.0 - fv.probability.value),
                standard_error=fv.standard_error,
                as_of=fv.as_of,
                method=fv.method,
                sources=fv.sources,
            )
    return extended


class ValueDetector:
    """Detector for the 'mispriced vs fair value' edge type.

    Listings whose outcome (or its complement) has no fair value are
    skipped: no fair value, no bet.
    """

    def __init__(
        self,
        fees: Mapping[VenueId, FeeModel],
        cfg: DetectorConfig,
        refs: str = "",
    ) -> None:
        self._fees = fees
        self._cfg = cfg
        self._refs = refs

    def detect(
        self,
        books: list[tuple[Listing, OrderBook]],
        fair_values: dict[Outcome, FairValue],
    ) -> list[Opportunity]:
        extended = _with_complements(fair_values)
        found: list[Opportunity] = []
        for listing, book in books:
            fee_model = self._fees.get(listing.key.venue_id)
            if fee_model is None:
                continue
            fair = extended.get(listing.outcome)
            if fair is None:
                continue
            opp = evaluate(listing, book, fair, fee_model, self._cfg, self._refs)
            if opp is not None:
                found.append(opp)
        return found
