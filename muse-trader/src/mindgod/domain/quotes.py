"""Observed prices. Immutable, append-only facts stamped with an Observation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from mindgod.domain.primitives import ContractPrice, Observation, Probability
from mindgod.domain.venues import ListingKey


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: ContractPrice
    contracts: int

    def __post_init__(self) -> None:
        if self.contracts <= 0:
            raise ValueError("a price level must have positive size")


@dataclass(frozen=True, slots=True)
class FillEstimate:
    """What filling `contracts` against current depth would cost or return, before fees."""

    contracts: int
    notional: Decimal
    worst_price: ContractPrice

    @property
    def average_price(self) -> Decimal:
        return self.notional / self.contracts


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Depth for one listing at one moment.

    Edge is a function of size, so we price against the whole ladder, never
    just the top of the book.
    """

    listing: ListingKey
    asks: tuple[PriceLevel, ...]  # where we buy: best (lowest) first
    bids: tuple[PriceLevel, ...]  # where we sell: best (highest) first
    observed: Observation

    def __post_init__(self) -> None:
        ask_prices = [level.price.dollars for level in self.asks]
        bid_prices = [level.price.dollars for level in self.bids]
        if ask_prices != sorted(ask_prices) or bid_prices != sorted(bid_prices, reverse=True):
            raise ValueError("asks must ascend and bids must descend")

    def cost_to_buy(self, contracts: int) -> FillEstimate | None:
        """None when the book is too thin to fill `contracts`."""
        return _walk(self.asks, contracts)

    def proceeds_from_selling(self, contracts: int) -> FillEstimate | None:
        return _walk(self.bids, contracts)


def _walk(levels: tuple[PriceLevel, ...], contracts: int) -> FillEstimate | None:
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    remaining, notional = contracts, Decimal(0)
    for level in levels:
        taken = min(remaining, level.contracts)
        notional += taken * level.price.dollars
        remaining -= taken
        if remaining == 0:
            return FillEstimate(contracts, notional, level.price)
    return None


@dataclass(frozen=True, slots=True)
class SportsbookQuote:
    """A house price. Vig is included, so it is an input to devigging, not a fair value."""

    listing: ListingKey
    american_odds: int
    observed: Observation

    @property
    def implied_probability(self) -> Probability:
        return Probability.from_american_odds(self.american_odds)
