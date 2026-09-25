"""Risk policy: portfolio-level guards before any order.

Positions on the same game are one correlated bet, so exposure is capped
per event. Daily loss limits need settlement data to close positions and
stay a documented gap until settlement feeds are wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from .opportunities import Opportunity
from .ports import Fill


class RiskPolicy(Protocol):
    def approve(self, opportunity: Opportunity) -> bool: ...
    def record_fill(self, fill: Fill) -> None: ...


@dataclass
class ExposureLimits:
    """Cap total staked per event. Resets with the process; a persistent
    ledger belongs in the store once live trading exists."""

    max_exposure_per_event: Decimal = Decimal("500")
    _exposure: dict[str, Decimal] = field(default_factory=dict)

    def approve(self, opportunity: Opportunity) -> bool:
        event_id = str(opportunity.outcome.quantity.event_id)
        used = self._exposure.get(event_id, Decimal(0))
        return used + opportunity.stake <= self.max_exposure_per_event

    def record_fill(self, fill: Fill) -> None:
        event_id = str(fill.outcome.quantity.event_id)
        cost = fill.fill_price * fill.contracts
        self._exposure[event_id] = self._exposure.get(event_id, Decimal(0)) + cost
