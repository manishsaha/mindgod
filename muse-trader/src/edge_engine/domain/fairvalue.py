"""Fair value with confidence.

A fair probability without an error bar is a guess wearing a number. The
confidence score blends four things the research calls out: how much of the
weight comes from sharp books, how many books contribute, how much the books
agree, and how close the event is. Downstream, confidence shrinks edge
thresholds and bet sizes; it never lives only in a log line.

Note the honest weakness: with a single source, agreement is perfect by
definition. The breadth term is what punishes that case.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..pricing.devig import consensus


@dataclass(frozen=True)
class FairSource:
    venue: str
    weight: float
    prob: float               # de-vigged fair probability from this book


@dataclass(frozen=True)
class FairValue:
    canonical_market_id: str
    outcome_id: str
    prob: float               # weighted consensus
    confidence: float         # 0..1
    method: str
    sources: tuple[FairSource, ...]
    computed_at: datetime


def build_fair_value(
    canonical_market_id: str,
    outcome_id: str,
    sources: list[FairSource],
    method: str = "weighted-consensus",
    sharp_venues: frozenset[str] = frozenset({"pinnacle"}),
    starts_at: datetime | None = None,
    now: datetime | None = None,
) -> FairValue:
    if not sources:
        raise ValueError("need at least one source")
    prob = consensus([(s.prob, s.weight) for s in sources])

    total_w = sum(s.weight for s in sources)
    sharp_share = sum(s.weight for s in sources if s.venue in sharp_venues) / total_w
    breadth = min(1.0, len(sources) / 4)
    variance = sum(s.weight * (s.prob - prob) ** 2 for s in sources) / total_w
    agreement = max(0.0, 1.0 - variance**0.5 / 0.1)
    base = 0.4 * sharp_share + 0.3 * breadth + 0.3 * agreement

    if starts_at is not None and now is not None:
        hours = max(0.0, (starts_at - now).total_seconds() / 3600)
        time_factor = 1.0 / (1.0 + hours / 24.0)
    else:
        time_factor = 0.7  # unknown timing: discount

    return FairValue(
        canonical_market_id=canonical_market_id,
        outcome_id=outcome_id,
        prob=round(prob, 4),
        confidence=round(min(1.0, base * time_factor), 4),
        method=method,
        sources=tuple(sources),
        computed_at=now or datetime.now(),
    )
