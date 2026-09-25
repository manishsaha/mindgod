"""Order execution.

Modes: dry-run (log only, default), paper (simulate fills), live (real
orders, requires explicit enablement). Nothing here places a real order
unless a live venue is constructed with live=True AND the config enables it;
the live venues below fail closed until their order paths are implemented
and explicitly authorized.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from mindgod.application.opportunities import Opportunity
from mindgod.application.ports import ExecutionVenue, Fill
from mindgod.domain.venues import ListingKey

log = logging.getLogger("mindgod.execution")


class DryRunExecution(ExecutionVenue):
    name = "dry-run"

    async def buy(self, opportunity: Opportunity) -> Fill | None:
        key = opportunity.listing
        log.info(
            "DRY-RUN would buy %s %s %s @ avg %s stake $%s",
            key.side,
            key.venue_id,
            key.market_id,
            opportunity.fill.average_price,
            opportunity.stake,
        )
        return None


class PaperExecution(ExecutionVenue):
    """Simulate fills at the estimated average price; track paper P&L.

    Simulated liquidity is consumed. The detector caps each fill at the
    depth it saw, so the largest requested fill is a lower bound on the
    book's depth; fills accumulate against that bound per listing. Once the
    simulated depth is exhausted, further fills wait for a deeper book.
    Without this, every tick would refill the same contracts forever.
    """

    name = "paper"

    def __init__(self) -> None:
        self.fills: list[Fill] = []
        self._depth: dict[ListingKey, int] = {}
        self._consumed: dict[ListingKey, int] = {}

    async def buy(self, opportunity: Opportunity) -> Fill | None:
        key = opportunity.listing
        want = opportunity.fill.contracts
        known = self._depth.get(key, 0)
        if want > known:
            known = want
            self._depth[key] = known
        remaining = known - self._consumed.get(key, 0)
        take = min(want, remaining)
        if take <= 0:
            log.info(
                "PAPER no simulated depth left for %s; skipping",
                key.market_id,
            )
            return None
        fill = Fill(
            listing=opportunity.listing,
            outcome=opportunity.outcome,
            contracts=take,
            fill_price=opportunity.fill.average_price,
            fee=opportunity.fee,
            live=False,
            at=datetime.now(UTC),
        )
        self.fills.append(fill)
        self._consumed[key] = self._consumed.get(key, 0) + take
        log.info(
            "PAPER fill: %s %s %s contracts @ %s",
            key.side,
            key.market_id,
            fill.contracts,
            fill.fill_price,
        )
        return fill


class KalshiExecution(ExecutionVenue):
    name = "kalshi"

    def __init__(self, live: bool = False) -> None:
        if live:
            raise RuntimeError(
                "Live Kalshi trading is not wired yet: implement request "
                "signing (RSA-PSS or Ed25519, key-dependent) and "
                "POST /portfolio/orders first."
            )

    async def buy(self, opportunity: Opportunity) -> Fill | None:
        raise NotImplementedError("Kalshi order placement not implemented")


class PolymarketExecution(ExecutionVenue):
    name = "polymarket"

    def __init__(self, live: bool = False) -> None:
        if live:
            raise RuntimeError(
                "Live Polymarket trading is not wired yet: implement EIP-712 "
                "auth and CLOB POST /order first. US accounts face geo "
                "restrictions; verify eligibility."
            )

    async def buy(self, opportunity: Opportunity) -> Fill | None:
        raise NotImplementedError("Polymarket order placement not implemented")


def make_execution(mode: str, live_flag: bool = False) -> ExecutionVenue:
    if mode == "dry-run":
        return DryRunExecution()
    if mode == "paper":
        return PaperExecution()
    if mode == "live":
        if not live_flag:
            raise RuntimeError(
                "Live mode requires both config execution.mode=live and the "
                "--live command-line flag."
            )
        raise RuntimeError("No live execution venue is wired yet.")
    raise ValueError(f"unknown execution mode: {mode}")
