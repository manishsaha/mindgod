"""Service: the tick loop that turns observations into opportunities.

One tick: pull sportsbook prices, build fair values, pull exchange books for
registered listings, detect, then notify and execute per opportunity. Markets
we cannot map go to the resolver's review queue; they are never traded.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from mindgod.domain.venues import VenueId

from .opportunities import Opportunity
from .ports import (
    ExchangeSource,
    ExecutionVenue,
    FairValueModel,
    ListingResolver,
    Notifier,
    ObservationStore,
    OpportunityDetector,
    SportsbookSource,
    UnmappedMarket,
)
from .risk import RiskPolicy

log = logging.getLogger("mindgod.service")


@dataclass
class ServiceContext:
    sportsbook: SportsbookSource | None
    exchanges: list[ExchangeSource]
    execution: dict[VenueId, ExecutionVenue]
    resolver: ListingResolver
    model: FairValueModel
    detector: OpportunityDetector
    risk: RiskPolicy
    store: ObservationStore
    notifier: Notifier | None = None


async def tick(ctx: ServiceContext) -> list[Opportunity]:
    now = datetime.now(UTC)
    priced = await ctx.sportsbook.priced_outcomes() if ctx.sportsbook else []
    ctx.store.record_priced(priced, now)
    fair_values = ctx.model.value(priced, now)

    found: list[Opportunity] = []
    for exchange in ctx.exchanges:
        listings = ctx.resolver.listings_for(exchange.venue_id)
        books = await exchange.order_books(listings)
        ctx.store.record_books(books, now)
        for market in await exchange.discover():
            ctx.resolver.report_unmapped(market)
        known = []
        for book in books:
            listing = ctx.resolver.resolve(book.listing)
            if listing is None:
                ctx.resolver.report_unmapped(
                    UnmappedMarket(
                        venue_id=exchange.venue_id,
                        market_id=book.listing.market_id,
                        label=book.listing.side,
                        seen_at=now,
                        reason="book for an unregistered listing",
                    )
                )
                continue
            known.append((listing, book))
        for opp in ctx.detector.detect(known, fair_values):
            if not ctx.risk.approve(opp):
                log.info("risk rejected %s", opp.listing)
                continue
            ctx.store.record_opportunity(opp, now)
            found.append(opp)
            if ctx.notifier is not None:
                await ctx.notifier.send(opp)
            venue = ctx.execution.get(opp.listing.venue_id)
            if venue is None:
                log.warning("no execution venue for %s", opp.listing.venue_id)
                continue
            fill = await venue.buy(opp)
            if fill is not None:
                ctx.store.record_fill(fill)
                ctx.risk.record_fill(fill)
    if ctx.resolver.review_queue:
        log.info("%d markets awaiting review", len(ctx.resolver.review_queue))
    return found


async def run_forever(ctx: ServiceContext, interval_s: int) -> None:
    while True:
        try:
            opps = await tick(ctx)
            log.info("tick done: %d opportunities", len(opps))
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(interval_s)
