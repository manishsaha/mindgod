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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from mindgod.domain.quotes import OrderBook
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import ListingKey, VenueId

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
from .pricing import terms_key
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
    # Move check: suppress an opportunity when the exchange mid moved more
    # than this (in probability) since the sportsbook prices were refreshed.
    # News reprices the exchange in seconds while the consensus lags by
    # minutes; without the check we would flag the correct new price as
    # mispriced.
    max_kalshi_move: float = 0.03
    mid_at_refresh: dict[ListingKey, float] = field(default_factory=dict)
    sportsbook_refreshed: bool = False
    # Notification tasks are held here so the event loop cannot garbage
    # collect a fire-and-forget send before it runs.
    notify_tasks: set[asyncio.Task[None]] = field(default_factory=set)


async def _notify_safely(notifier: Notifier, opp: Opportunity) -> None:
    try:
        await notifier.send(opp)
    except Exception:
        log.exception("discord notify failed for %s", opp.listing)


def _spawn_notify(ctx: ServiceContext, notifier: Notifier, opp: Opportunity) -> None:
    """Fire-and-forget without the garbage collector eating the task."""
    task = asyncio.create_task(_notify_safely(notifier, opp))
    ctx.notify_tasks.add(task)
    task.add_done_callback(ctx.notify_tasks.discard)


def _mid_price(book: OrderBook) -> float | None:
    """Best-bid/best-ask mid in probability, or None for an empty book."""
    ask = book.asks[0].price.dollars if book.asks else None
    bid = book.bids[0].price.dollars if book.bids else None
    if ask is not None and bid is not None:
        return float((ask + bid) / 2)
    if ask is not None:
        return float(ask)
    if bid is not None:
        return float(bid)
    return None


async def tick(ctx: ServiceContext, priced: list[PricedOutcome]) -> list[Opportunity]:
    now = datetime.now(UTC)
    ctx.store.record_priced(priced, now)
    # Devigging ran per market group inside the model; the partitions here
    # only keep a push-refunding book line from informing a no-push listing.
    fair_by_terms = ctx.model.values_by_terms(priced, now)

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
        if ctx.sportsbook_refreshed:
            # Anchor for the move check: the exchange mid at the moment the
            # fresh sportsbook prices arrived.
            for listing, book in zip(listings, books, strict=True):
                mid = _mid_price(book)
                if mid is not None:
                    ctx.mid_at_refresh[listing.key] = mid
        known = []
        for listing, book in zip(listings, books, strict=True):
            start = ctx.resolver.event_start(listing.key)
            if start is not None and start > horizon:
                continue
            known.append((listing, book))
        for listing, book in known:
            ref_mid = ctx.mid_at_refresh.get(listing.key)
            mid = _mid_price(book)
            if ref_mid is not None and mid is not None and abs(mid - ref_mid) > ctx.max_kalshi_move:
                log.info(
                    "suppressing %s: exchange mid moved %.3f since the sportsbook"
                    " refresh; the consensus is stale",
                    listing.key,
                    abs(mid - ref_mid),
                )
                continue
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
                    _spawn_notify(ctx, ctx.notifier, opp)
    ctx.sportsbook_refreshed = False
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
                    ctx.sportsbook_refreshed = True
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
