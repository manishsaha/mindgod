"""Settlement rules as first-class data.

Many apparent arbitrages are actually two different bets: one venue includes
overtime, the other does not; one voids on a DNP, the other grades it a loss.
The fingerprint lets the mapper compare rule sets cheaply. Two markets are
only comparable when their fingerprints match or an explicit equivalence is
registered. A mismatch quarantines the market: priced, never signaled.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SettlementRules:
    overtime_included: bool = True
    void_on_dnp: bool = True          # player props void if the player does not play
    stat_correction_window_h: float = 24.0
    postponement_void_h: float = 48.0  # void if postponed beyond this window
    dead_heat: bool = False
    source: str = ""                  # e.g. kalshi:rulebook, polymarket:rules
    notes: str = ""                   # free-text excerpt of the actual rule

    def fingerprint(self) -> str:
        payload = "|".join([
            str(self.overtime_included),
            str(self.void_on_dnp),
            str(self.stat_correction_window_h),
            str(self.postponement_void_h),
            str(self.dead_heat),
            self.source.strip().lower(),
        ])
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
