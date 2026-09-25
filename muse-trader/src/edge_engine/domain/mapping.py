"""Entity resolution: link venue markets to canonical markets.

Explicit registration is the source of truth. suggest() proposes candidates
for human confirmation when bootstrapping new mappings; resolve() never
guesses. A venue quote whose settlement fingerprint disagrees with the
registered one is quarantined: it stays priced but is never signaled, because
it may be a different bet wearing a similar name.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .markets import CanonicalMarket


def _tokens(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _name_score(query: str, candidate: str) -> float:
    q, c = _tokens(query), _tokens(candidate)
    if not q:
        return 0.0
    recall = len(q & c) / len(q)
    seq = difflib.SequenceMatcher(
        None, " ".join(sorted(q)), " ".join(sorted(c))).ratio()
    return 0.5 * recall + 0.5 * seq


@dataclass(frozen=True)
class RuleMismatch:
    venue: str
    venue_market_id: str
    canonical_market_id: str
    expected_fingerprint: str | None
    observed_fingerprint: str | None
    at: datetime


class MarketMapper:
    def __init__(self) -> None:
        self._markets: dict[str, CanonicalMarket] = {}
        self._links: dict[tuple[str, str], str] = {}
        self._fingerprints: dict[tuple[str, str], str | None] = {}
        self.mismatches: list[RuleMismatch] = []

    def register(
        self,
        market: CanonicalMarket,
        venue_links: dict[str, tuple[str, str | None]],
    ) -> None:
        """Register a canonical market.

        venue_links maps venue -> (venue_market_id, settlement fingerprint).
        A None fingerprint means "rules not yet captured"; the first observed
        fingerprint is adopted, later changes quarantine the link.
        """
        self._markets[market.market_id] = market
        for venue, (venue_market_id, fingerprint) in venue_links.items():
            key = (venue, venue_market_id)
            self._links[key] = market.market_id
            if fingerprint is not None or key not in self._fingerprints:
                self._fingerprints[key] = fingerprint

    def resolve(
        self,
        venue: str,
        venue_market_id: str,
        rules_fingerprint: str | None = None,
    ) -> CanonicalMarket | None:
        """Return the canonical market for a venue market id, or None.

        None means unknown (needs mapping) or quarantined (settlement rules
        changed under us). Quarantines are recorded in self.mismatches.
        """
        key = (venue, venue_market_id)
        market_id = self._links.get(key)
        if market_id is None:
            return None
        if rules_fingerprint is not None:
            expected = self._fingerprints.get(key)
            if expected is None:
                self._fingerprints[key] = rules_fingerprint
            elif expected != rules_fingerprint:
                self.mismatches.append(RuleMismatch(
                    venue=venue,
                    venue_market_id=venue_market_id,
                    canonical_market_id=market_id,
                    expected_fingerprint=expected,
                    observed_fingerprint=rules_fingerprint,
                    at=datetime.now(timezone.utc),
                ))
                return None
        return self._markets[market_id]

    def suggest(
        self,
        label: str,
        starts_at: datetime | None = None,
        limit: int = 5,
    ) -> list[tuple[CanonicalMarket, float]]:
        """Ranked mapping candidates for a venue market label. For human
        confirmation, not for automatic linking."""
        scored: list[tuple[CanonicalMarket, float]] = []
        for market in self._markets.values():
            score = _name_score(label, market.label)
            if starts_at is not None and market.starts_at is not None:
                hours = abs((market.starts_at - starts_at).total_seconds()) / 3600
                date_score = max(0.0, 1.0 - hours / 48.0)
                score = 0.7 * score + 0.3 * date_score
            scored.append((market, round(score, 4)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]
