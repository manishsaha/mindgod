"""EV engine: fee-aware edge detection and fractional-Kelly sizing.

The edge gate is applied to NET edge (gross edge minus per-contract taker
fee minus slippage), never to gross edge. A flat threshold on gross edge is
wrong because the taker fee is price-dependent: it peaks at 50c and shrinks
toward the tails. Rounding-up per order also means tiny orders pay a much
higher effective rate; the net-edge gate kills those automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..pricing.fees import FeeSchedule


@dataclass(frozen=True)
class Signal:
    event_key: str
    venue: str
    side: str                # "yes" or "no"
    market_price: float      # price to buy the contract, in [0,1]
    fair_prob: float         # consensus fair probability
    edge: float              # NET edge per contract: fair - price - fee - slippage
    ev_per_dollar: float     # expected profit per $1 staked, net of fee + slippage
    stake: float
    fee_per_contract: float = 0.0
    refs: str = ""           # bookmaker references for audit


def ev_per_contract(fair_prob: float, price: float, fee_per_contract: float = 0.0) -> float:
    """Expected profit of buying one $1-payout contract at `price`.

    EV = fair_prob * (1 - price) - (1 - fair_prob) * price - fee
       = fair_prob - price - fee
    """
    return fair_prob - price - fee_per_contract


def kelly_fraction(fair_prob: float, price: float) -> float:
    """Full-Kelly fraction of bankroll to stake on buying Yes at `price`.

    Derived: f* = (q - p) / (1 - p) for a contract costing p that pays 1.
    Returns 0 when there is no edge.
    """
    if price <= 0 or price >= 1:
        return 0.0
    edge = fair_prob - price
    if edge <= 0:
        return 0.0
    return edge / (1.0 - price)


def _stake_for_edge(edge: float, price: float, bankroll: float,
                    kelly_mult: float, max_stake: float) -> float:
    return min(kelly_fraction(edge + price, price) * kelly_mult * bankroll,
               max_stake)


def evaluate(
    event_key: str,
    venue: str,
    side: str,
    market_price: float,
    fair_prob: float,
    bankroll: float,
    min_net_edge: float,
    kelly_fraction_mult: float,
    max_stake: float,
    fee_schedule: FeeSchedule | None = None,
    slippage: float = 0.0,
    refs: str = "",
) -> Signal | None:
    """Build a Signal if the NET edge clears the threshold, else None.

    Two passes: size tentatively on gross edge to learn the order's contract
    count (which sets the per-contract fee under round-up), gate on net edge,
    then size finally on net edge.
    """
    if side == "no":
        market_price = 1.0 - market_price
        fair_prob = 1.0 - fair_prob
    if market_price <= 0 or market_price >= 1:
        return None

    gross_edge = fair_prob - market_price
    if gross_edge <= 0:
        return None

    # Pass 1: tentative size on gross edge -> contract count -> per-contract fee.
    tentative = _stake_for_edge(gross_edge, market_price, bankroll,
                                kelly_fraction_mult, max_stake)
    contracts = tentative / market_price if tentative > 0 else 0.0
    fee_pc = (fee_schedule.taker_fee_per_contract(contracts, market_price)
              if fee_schedule and contracts > 0 else 0.0)

    # The gate: net of fee and slippage. This is the price-dependent
    # threshold; a flat gross threshold would misprice 50c vs 90c markets.
    net_edge = gross_edge - fee_pc - slippage
    if net_edge < min_net_edge:
        return None

    # Pass 2: final sizing on net edge.
    stake = _stake_for_edge(net_edge, market_price, bankroll,
                            kelly_fraction_mult, max_stake)
    if stake <= 0:
        return None

    return Signal(
        event_key=event_key,
        venue=venue,
        side=side,
        market_price=round(market_price, 4),
        fair_prob=round(fair_prob, 4),
        edge=round(net_edge, 4),
        ev_per_dollar=round(net_edge / market_price, 4),
        stake=round(stake, 2),
        fee_per_contract=round(fee_pc, 4),
        refs=refs,
    )
