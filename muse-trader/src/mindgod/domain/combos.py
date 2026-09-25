"""Combos (parlays): pay only if every leg happens.

Market makers that price legs as independent, or with crude correlation,
leave some of the richest edges on the exchange.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from mindgod.domain.primitives import ContractPrice, Observation, Probability
from mindgod.domain.propositions import Outcome
from mindgod.domain.sports import EventId


@dataclass(frozen=True, slots=True)
class Combo:
    legs: frozenset[Outcome]

    def __post_init__(self) -> None:
        if len(self.legs) < 2:
            raise ValueError("a combo needs at least two legs")

    @property
    def event_ids(self) -> frozenset[EventId]:
        return frozenset(leg.quantity.event_id for leg in self.legs)

    @property
    def is_same_game(self) -> bool:
        return len(self.event_ids) == 1

    def independent_probability(self, legs: Mapping[Outcome, Probability]) -> Probability:
        """The naive price: product of the legs.

        The gap between this and a correlation-aware fair value is the edge a
        lazy quoter leaves on the table.
        """
        return Probability(math.prod(legs[leg].value for leg in self.legs))


@dataclass(frozen=True, slots=True)
class ComboQuote:
    combo: Combo
    price: ContractPrice
    quoter_id: str | None  # set when the venue identifies the counterparty; drives cooldowns
    observed: Observation
    expires_at: datetime | None = None
