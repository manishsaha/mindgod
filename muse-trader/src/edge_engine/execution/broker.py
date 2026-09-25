"""Order execution adapters.

Modes: dry-run (log only, default), paper (simulate fills), live (real orders,
requires explicit enablement). Nothing here places a real order unless a live
broker is constructed with live=True AND the config enables it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..engine.ev import Signal

log = logging.getLogger("edge_engine.execution")


@dataclass
class Fill:
    signal: Signal
    contracts: float
    fill_price: float
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    live: bool = False


class Broker:
    name = "base"

    async def place(self, signal: Signal) -> Fill | None:
        raise NotImplementedError


class DryRunBroker(Broker):
    name = "dry-run"

    async def place(self, signal: Signal) -> Fill | None:
        log.info("DRY-RUN would buy %s %s @ %.3f stake $%.2f",
                 signal.side, signal.event_key, signal.market_price,
                 signal.stake)
        return None


class PaperBroker(Broker):
    """Simulate fills at the quoted price; track paper P&L in memory."""

    name = "paper"

    def __init__(self) -> None:
        self.fills: list[Fill] = []

    async def place(self, signal: Signal) -> Fill | None:
        contracts = signal.stake / signal.market_price if signal.market_price else 0
        fill = Fill(signal=signal, contracts=contracts,
                    fill_price=signal.market_price, live=False)
        self.fills.append(fill)
        log.info("PAPER fill: %s %s %.1f contracts @ %.3f",
                 signal.side, signal.event_key, contracts, signal.market_price)
        return fill


class KalshiBroker(Broker):
    name = "kalshi"

    def __init__(self, live: bool = False) -> None:
        if live:
            raise RuntimeError(
                "Live Kalshi trading is not wired yet: implement Ed25519 "
                "signing and POST /portfolio/orders first."
            )

    async def place(self, signal: Signal) -> Fill | None:
        raise NotImplementedError("Kalshi order placement not implemented")


class PolymarketBroker(Broker):
    name = "polymarket"

    def __init__(self, live: bool = False) -> None:
        if live:
            raise RuntimeError(
                "Live Polymarket trading is not wired yet: implement EIP-712 "
                "auth and CLOB POST /order first. US accounts face geo "
                "restrictions; verify eligibility."
            )

    async def place(self, signal: Signal) -> Fill | None:
        raise NotImplementedError("Polymarket order placement not implemented")


def make_broker(mode: str, live_flag: bool = False) -> Broker:
    if mode == "dry-run":
        return DryRunBroker()
    if mode == "paper":
        return PaperBroker()
    if mode == "live":
        if not live_flag:
            raise RuntimeError(
                "Live mode requires both config execution.mode=live and the "
                "--live command-line flag."
            )
        raise RuntimeError("No live broker is wired yet.")
    raise ValueError(f"unknown execution mode: {mode}")
