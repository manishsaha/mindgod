"""Fair value: our estimate of an outcome's true probability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from mindgod.domain.primitives import Probability
from mindgod.domain.propositions import Outcome
from mindgod.domain.quotes import FillEstimate
from mindgod.domain.venues import ListingKey


@dataclass(frozen=True, slots=True)
class FairValue:
    """A probability with its uncertainty and lineage.

    Uncertainty widens edge thresholds and shrinks Kelly sizing. Lineage (the
    quotes it came from) makes every decision reproducible.
    """

    outcome: Outcome
    probability: Probability
    standard_error: float
    as_of: datetime
    method: str  # e.g. "power-devig:pinnacle,circa"
    sources: tuple[ListingKey, ...] = ()

    def expected_value_per_contract(self, fill: FillEstimate, fee: Decimal) -> Decimal:
        """Expected profit per $1 contract when buying at `fill`, net of `fee`."""
        fair = Decimal(str(self.probability.value))
        return fair - fill.average_price - fee / fill.contracts
