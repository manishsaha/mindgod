"""Service: the tick loop that turns observations into opportunities.

One tick: build fair values from the cached sportsbook prices (partitioned by
settlement terms, so a push-refunding line never informs a no-push listing),
pull exchange books for registered listings, detect, then execute and notify
per opportunity. Markets we cannot map go to the resolver's review queue;
they are never traded.

Polling intervals are separate on purpose: exchange books move every minute,
sportsbook odds cost credits per call, and discovery only feeds the review
queue. A sportsbook failure keeps the previous prices instead of killing the
tick.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mindgod.domain.terms import Payoff, Terms
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
    PricedOutcome,
    SportsbookSource,
)
from .pricing import partition_by_terms, terms_key
from .risk import RiskPolicy

log = logging.getLogger("mindgod.service")

_EVENT_BASE = re.compile(r"-g\d+$")


def _event_base(event_id: str) -> str:
    """Event identity without the game number: league, teams, date."""
    return _EVENT_BASE.sub("", event_id)


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
    horizon_days: int = 7


async def _notify_safely(notifier: Notifier, opp: Opportunity) -> None:
    try:
        await notifier.send(opp)
    except Exception:
        log.exception("discord notify failed for %s", opp.listing)


async def tick(ctx: ServiceContext, priced: list[PricedOutcome]) -> list[Opportunity]:
    now = datetime.now(UTC)
    ctx.store.record_priced(priced, now)
    # Devig runs inside each terms partition, so consensus never mixes a
    # push-refunding book line with a no-push line on the same outcome.
    fair_by_terms = {
        key: ctx.model.value(bucket, now) for key, bucket in partition_by_terms(priced).items()
    }

    listing_event_ids: set[str] = set()
    for exchange in ctx.exchanges:
        for listing in ctx.resolver.listings_for(exchange.venue_id):
            listing_event_ids.add(str(listing.outcome.quantity.event_id))
    listing_bases = {_event_base(e) for e in listing_event_ids}
    warned: set[str] = set()
    for p in priced:
        eid = str(p.outcome.quantity.event_id)
        if eid not in listing_event_ids and _event_base(eid) in listing_bases and eid not in warned:
            warned.add(eid)
            log.warning(
                "event identity drift: sportsbook event %s shares a listing's"
                " teams and date but not its game number; no fair value will"
                " be built for it",
                eid,
            )

    horizon = now + timedelta(days=ctx.horizon_days)
    found: list[Opportunity] = []
    for exchange in ctx.exchanges:
        listings = ctx.resolver.listings_for(exchange.venue_id)
        books = await exchange.order_books(listings)
        ctx.store.record_books(
            [(listing, book) for listing, book in zip(listings, books, strict=True)],
            now,
        )
        known = []
        for listing, book in zip(listings, books, strict=True):
            start = ctx.resolver.event_start(listing.key)
            if start is not None and start > horizon:
                continue
            known.append((listing, book))
        for listing, book in known:
            # Books price the yes side; a no-side listing's fair value is the
            # complement, built by the detector from the yes fair value. The
            # terms check runs on the yes basis: the refund rules are what
            # must match, and they are side-independent.
            yes_outcome = (
                listing.outcome if listing.key.side == "yes" else listing.outcome.complement()
            )
            if yes_outcome is None:
                continue
            key = terms_key(
                Terms(
                    Payoff(yes_outcome, listing.terms.payoff.refunds_if),
                    listing.terms.void_policy,
                )
            )
            fair = fair_by_terms.get(key, {}).get(yes_outcome)
            if fair is None:
                continue
            for opp in ctx.detector.detect([(listing, book)], {yes_outcome: fair}):
                if not ctx.risk.approve(opp):
                    log.info("risk rejected %s", opp.listing)
                    continue
                ctx.store.record_opportunity(opp, now)
                found.append(opp)
                venue = ctx.execution.get(opp.listing.venue_id)
                if venue is None:
                    log.warning("no execution venue for %s", opp.listing.venue_id)
                    continue
                try:
                    fill = await venue.buy(opp)
                except Exception:
                    log.exception("execution failed for %s", opp.listing)
                    continue
                if fill is not None:
                    ctx.store.record_fill(fill)
                    ctx.risk.record_fill(fill)
                # Notify after execution, off the critical path.
                if ctx.notifier is not None:
                    asyncio.create_task(_notify_safely(ctx.notifier, opp))
    if ctx.resolver.review_queue:
        log.info("%d markets awaiting review", len(ctx.resolver.review_queue))
    return found


async def _discover(ctx: ServiceContext) -> None:
    for exchange in ctx.exchanges:
        try:
            for market in await exchange.discover():
                ctx.resolver.report_unmapped(market)
        except Exception:
            log.exception("discovery failed for %s", exchange.venue_id)


async def run_forever(
    ctx: ServiceContext,
    exchange_interval_s: int = 60,
    sportsbook_interval_s: int = 300,
    discovery_interval_s: int = 3600,
) -> None:
    priced: list[PricedOutcome] = []
    last_sportsbook = 0.0
    last_discovery = 0.0
    while True:
        try:
            now_ts = time.monotonic()
            if ctx.sportsbook is not None and now_ts - last_sportsbook >= sportsbook_interval_s:
                try:
                    priced = await ctx.sportsbook.priced_outcomes()
                    last_sportsbook = now_ts
                except Exception:
                    log.exception(
                        "sportsbook refresh failed; keeping %d stale prices",
                        len(priced),
                    )
            opps = await tick(ctx, priced)
            if now_ts - last_discovery >= discovery_interval_s:
                await _discover(ctx)
                last_discovery = now_ts
            log.info("tick done: %d opportunities", len(opps))
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(exchange_interval_s)
