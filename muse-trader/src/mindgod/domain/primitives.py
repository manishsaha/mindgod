"""Value objects shared by the whole domain: probability, price, and time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Probability:
    """A probability in [0, 1]: the common unit for comparing prices across venues."""

    value: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"probability out of range: {self.value}")

    @classmethod
    def from_american_odds(cls, odds: int) -> Probability:
        """Implied probability of a sportsbook price, vig included (-110 -> 0.5238)."""
        if -100 < odds < 100:
            raise ValueError(f"invalid American odds: {odds}")
        if odds > 0:
            return cls(100 / (odds + 100))
        return cls(-odds / (-odds + 100))

    @classmethod
    def from_decimal_odds(cls, odds: float) -> Probability:
        if odds <= 1.0:
            raise ValueError(f"invalid decimal odds: {odds}")
        return cls(1 / odds)

    def complement(self) -> Probability:
        return Probability(1.0 - self.value)


@dataclass(frozen=True, slots=True)
class ContractPrice:
    """Dollar price of a binary contract that pays $1 if it wins, before fees."""

    dollars: Decimal

    def __post_init__(self) -> None:
        if not Decimal(0) < self.dollars < Decimal(1):
            raise ValueError(f"contract price must be strictly between 0 and 1: {self.dollars}")

    @property
    def implied_probability(self) -> Probability:
        return Probability(float(self.dollars))


@dataclass(frozen=True, slots=True)
class Observation:
    """Bitemporal stamp: when a fact held at the venue vs. when we recorded it.

    Keeping both lets us replay exactly what we knew at any moment (honest
    backtests, CLV) and measures feed latency per venue.
    """

    valid_at: datetime
    recorded_at: datetime

    def __post_init__(self) -> None:
        for stamp in (self.valid_at, self.recorded_at):
            if stamp.tzinfo is None:
                raise ValueError("observation timestamps must be timezone-aware")

    @property
    def feed_latency(self) -> timedelta:
        return self.recorded_at - self.valid_at
