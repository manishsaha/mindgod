"""Trading fees. EV is always computed net of these."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from mindgod.domain.primitives import ContractPrice

_CENT = Decimal("0.01")


class FeeModel(Protocol):
    def taker_fee(self, price: ContractPrice, contracts: int) -> Decimal: ...

    def maker_fee(self, price: ContractPrice, contracts: int) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class QuadraticFeeModel:
    """fee = ceil_to_cent(rate * contracts * P * (1 - P)), charged per order.

    Kalshi's published fee form: largest at 50c and rounded up per order, so
    small orders pay a higher effective rate. Rates differ by market and change
    over time, so they come from configuration and are never hard-coded.
    """

    taker_rate: Decimal
    maker_rate: Decimal

    def taker_fee(self, price: ContractPrice, contracts: int) -> Decimal:
        return self._fee(self.taker_rate, price, contracts)

    def maker_fee(self, price: ContractPrice, contracts: int) -> Decimal:
        return self._fee(self.maker_rate, price, contracts)

    @staticmethod
    def _fee(rate: Decimal, price: ContractPrice, contracts: int) -> Decimal:
        if contracts <= 0:
            raise ValueError("contracts must be positive")
        p = price.dollars
        return (rate * contracts * p * (1 - p)).quantize(_CENT, rounding=ROUND_CEILING)
