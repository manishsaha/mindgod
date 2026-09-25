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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from mindgod.domain.fees import FeeModel
from mindgod.domain.quotes import OrderBook
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing, ListingKey, VenueId

from .calls import (
    Call,
    build_call,
    instant_fill,
    reaction_fill,
    snapshot_for,
)
from .opportunities import DetectorConfig, Opportunity
from .ports import (
    ExchangeSource,
    ExecutionVenue,
    FairValueModel,
    ListingResolver,
    Notifier,
    ObservationStore,
    OpportunityDetector,
    PricedOutcome,
    ProbablePitcherSource,
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
    # Spread gate for the move check: on thin books the mid jumps with single
    # orders and would cause false suppressions. When the spread exceeds this,
    # the check is skipped.
    move_check_max_spread: float = 0.06
    mid_at_refresh: dict[ListingKey, float] = field(default_factory=dict)
    sportsbook_refreshed: bool = False
    # Notification tasks are held here so the event loop cannot garbage
    # collect a fire-and-forget send before it runs.
    notify_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # ADR-0008 alert-first: paper fills model human reaction time.
    fees: Mapping[VenueId, FeeModel] = field(default_factory=dict)
    detector_cfg: DetectorConfig | None = None
    reaction_delay_s: int = 60
    # Background tasks for decay snapshots and reaction fills.
    call_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # ADR-0009: extra SE for UNKNOWN/LISTED vs ACTION pitcher rules.
    pitcher_rule_se: float = 0.01
    # ADR-0009: probable pitcher tracking. Maps event_id to (home, away) tuple.
    # When probables change or are missing, MLB listings are suppressed.
    probables: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    # Event IDs with pitcher changes pending a sportsbook refresh.
    pitcher_suppressed: set[str] = field(default_factory=set)
    # ADR-0009: source for probable pitchers (optional).
    probable_source: ProbablePitcherSource | None = None


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


async def _update_probables(ctx: ServiceContext) -> None:
    """ADR-0009: fetch probable pitchers and detect changes.

    When probables change, or fewer than two are announced, the game's MLB
    listings are suppressed until the next sportsbook refresh.
    """
    if ctx.probable_source is None:
        return

    # Collect MLB event IDs from listings
    mlb_event_ids: set[str] = set()
    for exchange in ctx.exchanges:
        for listing in ctx.resolver.listings_for(exchange.venue_id):
            if listing.key.market_id.startswith("KXMLBGAME"):
                try:
                    eid = str(listing.outcome.quantity.event_id)
                    mlb_event_ids.add(eid)
                except AttributeError:
                    continue

    if not mlb_event_ids:
        return

    try:
        probables = await ctx.probable_source.probables(list(mlb_event_ids))
    except Exception:
        log.exception("failed to fetch probable pitchers")
        return

    for p in probables:
        eid = p.event_id
        new_pair = (p.home_pitcher, p.away_pitcher)
        old_pair = ctx.probables.get(eid)

        # Check if probables changed or are incomplete
        if old_pair != new_pair:
            if old_pair is not None:
                log.info(
                    "probable pitchers changed for %s: %s -> %s",
                    eid,
                    old_pair,
                    new_pair,
                )
            ctx.probables[eid] = new_pair

        # Suppress if fewer than two announced
        if p.home_pitcher is None or p.away_pitcher is None:
            if eid not in ctx.pitcher_suppressed:
                log.info(
                    "suppressing %s: missing probable pitchers (home=%s, away=%s)",
                    eid,
                    p.home_pitcher,
                    p.away_pitcher,
                )
                ctx.pitcher_suppressed.add(eid)
        elif old_pair != new_pair and old_pair is not None:
            # Changed: suppress until next refresh
            ctx.pitcher_suppressed.add(eid)


def _is_pitcher_suppressed(listing: Listing, ctx: ServiceContext) -> bool:
    """ADR-0009: check if an MLB listing is suppressed due to pitcher news.

    Returns True if the listing is for an MLB game whose probables have
    changed or are not fully announced, and the suppression has not been
    cleared by a sportsbook refresh.
    """
    # Only applies to MLB listings (Kalshi KXMLBGAME series)
    if not listing.key.market_id.startswith("KXMLBGAME"):
        return False

    # Get event_id from the outcome
    # The outcome's quantity has the event_id
    try:
        event_id = str(listing.outcome.quantity.event_id)
    except AttributeError:
        return False

    return event_id in ctx.pitcher_suppressed


def _spread(book: OrderBook) -> float | None:
    """Best ask minus best bid, or None when the book is one-sided."""
    if book.asks and book.bids:
        return float(book.asks[0].price.dollars - book.bids[0].price.dollars)
    return None


def _apply_pitcher_rule_adjustment(
    fair: FairValue,
    listing: Listing,
    ctx: ServiceContext,
    priced: list[PricedOutcome],
) -> FairValue | None:
    """ADR-0009: check pitcher rule compatibility and adjust SE.

    Returns the fair value (possibly with widened SE) if compatible,
    None if the pitcher rules are incompatible.
    """
    import math

    from mindgod.domain.terms import PitcherRule, pitcher_rules_compatible

    listing_rule = listing.terms.void_policy.pitcher_rule
    # Find the pitcher rules of the sources that built this fair value.
    # For now, we check if any priced outcome for this outcome has a
    # non-ACTION rule.
    source_rules = set()
    for p in priced:
        if p.outcome == fair.outcome:
            source_rules.add(p.terms.void_policy.pitcher_rule)

    # If all sources are ACTION and listing is ACTION, no adjustment.
    if not source_rules or all(r == PitcherRule.ACTION for r in source_rules):
        if listing_rule == PitcherRule.ACTION:
            return fair
        # Listing has non-ACTION but sources are ACTION: check compatibility
        for r in source_rules:
            if not pitcher_rules_compatible(r, listing_rule):
                log.info(
                    "rejecting %s: pitcher rule %s incompatible with %s",
                    listing.key,
                    r,
                    listing_rule,
                )
                return None
        return fair

    # Sources include UNKNOWN or LISTED.
    for source_rule in source_rules:
        if not pitcher_rules_compatible(source_rule, listing_rule):
            log.info(
                "rejecting %s: pitcher rule %s incompatible with %s",
                listing.key,
                source_rule,
                listing_rule,
            )
            return None

    # Compatible with adjustment: UNKNOWN/LISTED vs ACTION.
    needs_adjustment = (
        any(r in (PitcherRule.UNKNOWN, PitcherRule.LISTED) for r in source_rules)
        and listing_rule == PitcherRule.ACTION
    )

    if needs_adjustment:
        # Get pitcher_rule_se from pricing config via the model's settings.
        # For now, use the default 0.01; the service context should carry it.
        pitcher_se = 0.01
        # Try to get from ctx if available (added to ServiceContext later)
        if hasattr(ctx, "pitcher_rule_se"):
            pitcher_se = ctx.pitcher_rule_se
        new_se = math.sqrt(fair.standard_error**2 + pitcher_se**2)
        log.info(
            "widening SE for %s: %.4f -> %.4f (pitcher rule adjustment)",
            listing.key,
            fair.standard_error,
            new_se,
        )
        # Return a new FairValue with adjusted SE
        from dataclasses import replace

        return replace(fair, standard_error=new_se)

    return fair


async def _call_snapshot_task(
    ctx: ServiceContext,
    call: Call,
    listing: Listing,
    exchange: ExchangeSource,
    offset_s: int,
) -> None:
    """Record a decay snapshot at offset_s after the alert."""
    try:
        await asyncio.sleep(offset_s)
        books = await exchange.order_books([listing])
        if not books:
            return
        book = books[0]
        snap = snapshot_for(call, book, offset_s, datetime.now(UTC))
        try:
            ctx.store.record_call_snapshot(snap)
        except Exception:
            log.exception("record_call_snapshot failed for %s", call.call_id)
    except Exception:
        log.exception("snapshot task failed for %s", call.call_id)


async def _call_reaction_task(
    ctx: ServiceContext,
    call: Call,
    listing: Listing,
    exchange: ExchangeSource,
    fair: FairValue,
    fees: FeeModel,
    cfg: DetectorConfig,
) -> None:
    """Paper fill against the book observed reaction_delay_s after the alert."""
    try:
        await asyncio.sleep(ctx.reaction_delay_s)
        books = await exchange.order_books([listing])
        if not books:
            return
        book = books[0]
        fill = reaction_fill(call, book, fees, cfg, fair, datetime.now(UTC))
        try:
            ctx.store.record_paper_fill(fill)
        except Exception:
            log.exception("record_paper_fill failed for %s", call.call_id)
        if not fill.filled:
            log.info("call %s: no fill at reaction time (edge gone)", call.call_id)
    except Exception:
        log.exception("reaction fill task failed for %s", call.call_id)


def _spawn_call_tasks(
    ctx: ServiceContext,
    call: Call,
    listing: Listing,
    exchange: ExchangeSource,
    fair: FairValue,
    fees: FeeModel,
    cfg: DetectorConfig,
) -> None:
    """Schedule decay snapshots and the reaction fill, fire-and-forget."""
    for offset in (30, 120, 600):
        task = asyncio.create_task(_call_snapshot_task(ctx, call, listing, exchange, offset))
        ctx.call_tasks.add(task)
        task.add_done_callback(ctx.call_tasks.discard)
    task = asyncio.create_task(_call_reaction_task(ctx, call, listing, exchange, fair, fees, cfg))
    ctx.call_tasks.add(task)
    task.add_done_callback(ctx.call_tasks.discard)


async def tick(ctx: ServiceContext, priced: list[PricedOutcome]) -> list[Opportunity]:
    now = datetime.now(UTC)
    ctx.store.record_priced(priced, now)
    # Devigging ran per market group inside the model; the partitions here
    # only keep a push-refunding book line from informing a no-push listing.
    fair_by_terms = ctx.model.values_by_terms(priced, now)

    # ADR-0009: update probable pitchers for MLB games.
    await _update_probables(ctx)

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
            # ADR-0009: suppress MLB listings when probables changed or are
            # missing, until the next sportsbook refresh after the change.
            if _is_pitcher_suppressed(listing, ctx):
                log.info(
                    "suppressing %s: probable pitchers changed or unannounced",
                    listing.key,
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
            # ADR-0009: pitcher rule compatibility. The terms_key excludes the
            # pitcher rule, so MLB prices with UNKNOWN rule match ACTION
            # listings. When they do, widen the SE by pitcher_rule_se in
            # quadrature to account for the listed-pitcher uncertainty.
            fair = _apply_pitcher_rule_adjustment(fair, listing, ctx, priced)
            if fair is None:
                # Incompatible pitcher rules; skip this listing.
                continue
            # Directional move check: suppress only when the mid moved away
            # from fair value (widening the apparent edge) by more than the
            # threshold. A move toward fair shrinks the edge on its own and is
            # not evidence of a stale consensus. Skipped when the spread
            # exceeds the gate: on thin books the mid jumps with single orders.
            fair_prob = float(
                fair.probability.value
                if listing.key.side == "yes"
                else 1.0 - float(fair.probability.value)
            )
            ref_mid = ctx.mid_at_refresh.get(listing.key)
            mid = _mid_price(book)
            if ref_mid is not None and mid is not None:
                spread = _spread(book)
                if spread is None or spread > ctx.move_check_max_spread:
                    log.info(
                        "skipping move check for %s: spread %s exceeds gate %.3f",
                        listing.key,
                        "n/a" if spread is None else f"{spread:.3f}",
                        ctx.move_check_max_spread,
                    )
                elif abs(mid - fair_prob) - abs(ref_mid - fair_prob) > ctx.max_kalshi_move:
                    log.info(
                        "suppressing %s: exchange mid moved away from fair value "
                        "(%.3f -> %.3f vs fair %.3f); the consensus is stale",
                        listing.key,
                        ref_mid,
                        mid,
                        fair_prob,
                    )
                    continue
            for opp in ctx.detector.detect([(listing, book)], {yes_outcome: fair}):
                if not ctx.risk.approve(opp):
                    log.info("risk rejected %s", opp.listing)
                    continue
                ctx.store.record_opportunity(opp, now)
                found.append(opp)
                # ADR-0008 alert-first: every call is paper-tracked from the
                # first alert onward. Build the call, record the +0 snapshot
                # and the instant (reference) fill, then schedule decay
                # snapshots and the reaction-delay fill.
                if ctx.detector_cfg is not None:
                    fees_for_venue = ctx.fees.get(listing.key.venue_id)
                    if fees_for_venue is not None:
                        event_start = ctx.resolver.event_start(listing.key)
                        call = build_call(opp, book, event_start, now)
                        if call is not None:
                            try:
                                ctx.store.record_call(call)
                                ctx.store.record_call_snapshot(snapshot_for(call, book, 0, now))
                                ctx.store.record_paper_fill(
                                    instant_fill(
                                        call,
                                        book,
                                        fees_for_venue,
                                        ctx.detector_cfg,
                                        fair,
                                        now,
                                    )
                                )
                            except Exception:
                                log.exception("record call failed for %s", call.call_id)
                            else:
                                _spawn_call_tasks(
                                    ctx,
                                    call,
                                    listing,
                                    exchange,
                                    fair,
                                    fees_for_venue,
                                    ctx.detector_cfg,
                                )
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


def capture_closing_lines(
    ctx: ServiceContext,
    fair_by_terms: dict[str, dict[object, FairValue]],
    at: datetime,
) -> int:
    """ADR-0008: record the last sharp consensus before events lock.

    Called at event start. Returns the number of closing lines captured.
    """
    from .calls import ClosingLine
    from .pricing import outcome_key

    n = 0
    for listings in [ctx.resolver.listings_for(ex.venue_id) for ex in ctx.exchanges]:
        for listing in listings:
            start = ctx.resolver.event_start(listing.key)
            if start is None:
                continue
            # Capture if the event started within the last 5 minutes.
            if not (timedelta(0) <= at - start < timedelta(minutes=5)):
                continue
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
            prob = float(fair.probability.value)
            if listing.key.side == "no":
                prob = 1.0 - prob
            try:
                ctx.store.record_closing_line(
                    ClosingLine(
                        outcome_key=outcome_key(listing.outcome),
                        sharp_close_prob=prob,
                        source=fair.method,
                        captured_at=at,
                    )
                )
                n += 1
            except Exception:
                log.exception("record_closing_line failed for %s", listing.key)
    return n


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
                    # ADR-0009: clear pitcher suppression after a refresh; the
                    # new prices reflect the updated probables.
                    if ctx.pitcher_suppressed:
                        log.info(
                            "clearing pitcher suppression for %d events after sportsbook refresh",
                            len(ctx.pitcher_suppressed),
                        )
                        ctx.pitcher_suppressed.clear()
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
