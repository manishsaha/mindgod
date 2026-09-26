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
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from mindgod.domain.fees import FeeModel
from mindgod.domain.propositions import Outcome
from mindgod.domain.quotes import OrderBook
from mindgod.domain.sports import League
from mindgod.domain.terms import Payoff, PitcherRule, Terms, pitcher_rules_compatible
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import Listing, ListingKey, VenueId

from .calls import (
    Call,
    Settlement,
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
    OpsPoster,
    PricedOutcome,
    ProbablePitcherSource,
    SportsbookSource,
)
from .pricing import terms_key
from .risk import RiskPolicy
from .tie import find_tie_adjusted_fair, tie_aware_counterpart, tie_close_basis

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
    # Ops channel (DISCORD_WEBHOOK_OPS): credit burn and poll failures.
    # Never the calls webhook: quota telemetry must not page the trader.
    ops_poster: OpsPoster | None = None
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
    # Maps event_id -> change_time (when the change was detected).
    pitcher_suppressed: dict[str, datetime] = field(default_factory=dict)
    # ADR-0009: source for probable pitchers (optional).
    probable_source: ProbablePitcherSource | None = None
    # ADR-0008: in alert-first mode, do not execute via the venue. The call
    # system tracks paper fills with reaction time; the instant execution
    # path is for the old automatic mode only.
    alert_first: bool = True


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


async def _post_ops_safely(poster: OpsPoster, text: str) -> None:
    try:
        await poster.post(text)
    except Exception:
        log.exception("ops post failed")


def _post_ops(ctx: ServiceContext, text: str) -> None:
    """Fire-and-forget text to the ops channel; a no-op without a poster."""
    if ctx.ops_poster is None:
        return
    task = asyncio.create_task(_post_ops_safely(ctx.ops_poster, text))
    ctx.notify_tasks.add(task)
    task.add_done_callback(ctx.notify_tasks.discard)


def _report_quota(ctx: ServiceContext) -> None:
    """Post the Odds API credit burn to #ops after every poll."""
    if ctx.ops_poster is None or ctx.sportsbook is None:
        return
    quota = ctx.sportsbook.quota_status()
    if quota is None:
        return
    books = ",".join(quota.bookmakers) if quota.bookmakers else "none"
    _post_ops(
        ctx,
        f"Odds API poll: used {quota.used}, remaining {quota.remaining} (books: {books})",
    )


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
    listings are suppressed until the next sportsbook refresh after the change.
    """
    if ctx.probable_source is None:
        return

    # Collect MLB event IDs from listings
    mlb_event_ids: set[str] = set()
    for exchange in ctx.exchanges:
        for listing in ctx.resolver.listings_for(exchange.venue_id):
            try:
                if listing.outcome.quantity.league is League.MLB:
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

    now = datetime.now(UTC)
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
                ctx.pitcher_suppressed[eid] = now
        elif old_pair != new_pair and old_pair is not None:
            # Changed: suppress until next refresh (with change time tracked)
            ctx.pitcher_suppressed[eid] = now


def _is_pitcher_suppressed(listing: Listing, ctx: ServiceContext) -> bool:
    """ADR-0009: check if an MLB listing is suppressed due to pitcher news.

    Returns True if the listing is for an MLB game whose probables have
    changed or are not fully announced, and the suppression has not been
    cleared by a sportsbook refresh.
    """
    # Only applies to MLB listings
    try:
        if listing.outcome.quantity.league is not League.MLB:
            return False
    except AttributeError:
        return False

    # Get event_id from the outcome
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
        pitcher_se = ctx.pitcher_rule_se
        new_se = math.sqrt(fair.standard_error**2 + pitcher_se**2)
        log.info(
            "widening SE for %s: %.4f -> %.4f (pitcher rule adjustment)",
            listing.key,
            fair.standard_error,
            new_se,
        )
        # Return a new FairValue with adjusted SE
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
                # ADR-0011: the book's moneyline refunds on a tie while the
                # listing settles No. Convert the tie-refunding consensus
                # instead of rejecting on the terms mismatch.
                fair = find_tie_adjusted_fair(
                    fair_by_terms,
                    yes_outcome=yes_outcome,
                    listing_terms=listing.terms,
                    tie_prob=ctx.model.nfl_tie_prob,
                    tie_prob_se=ctx.model.nfl_tie_prob_se,
                )
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
                # ADR-0008: in alert-first mode, skip venue execution. The call
                # system records reaction-time paper fills; the instant fill
                # path must not feed risk metrics.
                if not ctx.alert_first:
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


def _close_for_outcome(
    store: ObservationStore,
    model: FairValueModel,
    outcome: Outcome,
    event_start: datetime,
    *,
    tie_prob: float | None = None,
) -> tuple[float | None, str]:
    """Devigged sharp consensus for one outcome, or (None, reason).

    Pairs the outcome with its exact complement per venue, devigs each
    pair (removing the vig), and takes a sharp-weighted consensus through
    the model port: the same math the live fair values use.

    Reason is one of "paired", "no_complement", "no_quotes", "no_pair",
    "stale", "devig_failed", "no_consensus". Fails closed throughout:
    whole-number lines don't pair, lone sides are skipped, and stale
    quotes (gated on confirmation age, recorded_at, per ADR-0007) are
    ignored.

    When tie_prob is set (ADR-0011), `outcome` must be the yes basis of an
    NFL moneyline: it pairs with the book's other side ("home margin <=
    -1", the tie-aware counterpart, not the exact complement "home margin
    <= 0" which the book never quotes), and the devigged P(win | no tie)
    is converted to the unconditional P(win).

    Shared by the live capture loop and the recompute migration so the
    two cannot drift apart.
    """
    from .pricing import devig, outcome_key

    if tie_prob is None:
        # Get the exact complement. If None (e.g., pushes/ties don't have
        # a clean complement), skip: fail closed.
        comp = outcome.complement()
        if comp is None:
            return None, "no_complement"
    else:
        # ADR-0011: pair the yes basis with the book's other moneyline
        # side. Anything that is not a moneyline shape fails closed.
        comp = tie_aware_counterpart(outcome)
        if comp is None:
            return None, "no_complement"
    okey = outcome_key(outcome)
    comp_key = outcome_key(comp)

    # Get latest quotes for both sides before event start.
    # Each entry is (venue, price, valid_at, recorded_at).
    own_quotes = store.latest_quotes_before(okey, event_start)
    comp_quotes = store.latest_quotes_before(comp_key, event_start)
    if not own_quotes or not comp_quotes:
        return None, "no_quotes"

    # Index by venue: {venue: (price, recorded_at)}
    mine = {v: (p, ra) for v, p, _, ra in own_quotes}
    theirs = {v: (p, ra) for v, p, _, ra in comp_quotes}
    shared = mine.keys() & theirs.keys()
    if not shared:
        return None, "no_pair"

    # Pair by venue and devig each pair. Staleness is gated on
    # confirmation age (recorded_at, per ADR-0007): a line that holds
    # steady before kickoff is still fresh as long as the feed kept
    # confirming it. If the feed died before kickoff, "the latest
    # quote before start" is not a closing line.
    max_age_s = model.max_quote_age_s
    method = model.devig_method
    venue_probs = []  # list of (venue, devigged_prob)
    saw_stale = False
    saw_devig_failure = False
    for venue in shared:
        p_own, ra_own = mine[venue]
        p_comp, ra_comp = theirs[venue]

        try:
            ra_own_dt = datetime.fromisoformat(ra_own)
            ra_comp_dt = datetime.fromisoformat(ra_comp)
        except (ValueError, TypeError):
            continue
        age_own = (event_start - ra_own_dt).total_seconds()
        age_comp = (event_start - ra_comp_dt).total_seconds()
        if age_own > max_age_s or age_comp > max_age_s:
            saw_stale = True
            continue

        # Devig the pair. If it fails (e.g., invalid probs), skip.
        try:
            devigged = devig([p_own, p_comp], method)
        except (ValueError, ZeroDivisionError):
            saw_devig_failure = True
            continue
        # devig returns [prob_own, prob_comp]; we want prob_own
        venue_probs.append((venue, devigged[0]))

    if not venue_probs:
        if saw_stale:
            return None, "stale"
        if saw_devig_failure:
            return None, "devig_failed"
        return None, "no_pair"

    # Sharp-weighted consensus through the model port: the same math
    # the live fair values use, so the two cannot drift apart.
    try:
        prob = model.consensus(venue_probs)
    except ValueError:
        return None, "no_consensus"
    if tie_prob is not None:
        # ADR-0011: the devigged pair is P(win | no tie); the listing
        # settles No on a tie, so convert to the unconditional P(win).
        prob = prob * (1.0 - tie_prob)
    return prob, "paired"


def capture_closing_lines(
    ctx: ServiceContext,
    at: datetime,
) -> int:
    """ADR-0008: record the last sharp consensus before events lock.

    For each listing, pairs the outcome with its exact complement per venue,
    devigs each pair (removing the vig), and takes a sharp-weighted consensus
    through the model port (the same math the live fair values use).

    The pairing fails closed:
    - Whole-number lines (e.g., KC -3) don't pair with the other side's
      whole-number line (BUF +3 is "margin <= 2", not the complement of
      "margin >= 4"), so those venues are skipped.
    - Lone sides (only one side quoted) are skipped, not passed through.
    - Stale quotes are ignored, gated on confirmation age (recorded_at, per
      ADR-0007): a line that holds steady before kickoff is still fresh as
      long as the feed kept confirming it.

    Each side is looked up under its own key, so there's nothing to flip:
    Yes and No listings get their own devigged probabilities directly.

    Called every loop; captures for any started event that has no closing
    line at the current method version. There is deliberately no one-hour
    window: the close reads stored history as of kickoff, so a late capture
    (e.g. after a restart during the first hour) computes the same value,
    and a failed capture is retried on later loops.
    Returns the number of closing lines captured.
    """
    from .calls import ClosingLine
    from .pricing import outcome_key

    n = 0
    for listings in [ctx.resolver.listings_for(ex.venue_id) for ex in ctx.exchanges]:
        for listing in listings:
            start = ctx.resolver.event_start(listing.key)
            if start is None:
                continue
            # Capture for any event that has started and has no close at the
            # current version. No one-hour window: late capture reads the
            # same stored pre-kickoff history, so it computes the same close.
            if at < start:
                continue

            # Check if already captured (version-aware: only the current
            # method counts).
            okey = outcome_key(listing.outcome)
            if ctx.store.has_closing_line(okey):
                continue

            # Each side is looked up under its own key, so there's nothing
            # to flip: Yes and No listings get their own devigged
            # probabilities directly.
            #
            # ADR-0011: an NFL moneyline listing settles No on a tie while
            # the book quotes refund it, so the exact complement never
            # pairs. Pair the yes basis under the tie rule instead; a
            # No-side listing's close is 1 - P(yes), which puts the tie
            # mass on the No side.
            tie_basis = tie_close_basis(listing)
            basis = tie_basis if tie_basis is not None else listing.outcome
            prob, _reason = _close_for_outcome(
                ctx.store,
                ctx.model,
                basis,
                start,
                tie_prob=ctx.model.nfl_tie_prob if tie_basis is not None else None,
            )
            if prob is None:
                continue
            if tie_basis is not None and listing.key.side == "no":
                prob = 1.0 - prob

            try:
                ctx.store.record_closing_line(
                    ClosingLine(
                        outcome_key=okey,
                        sharp_close_prob=prob,
                        source="pregame_consensus_devigged",
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


async def _check_settlements(ctx: ServiceContext) -> int:
    """ADR-0008: fetch market results from Kalshi and record settlements.

    For each unsettled call, get the market result and determine win/loss.
    Settlement only records the fact, once per outcome. Grading is a
    separate per-loop step (grade_pending_calls): grading here used to be a
    side effect of settlement, so a transient grading error left the call
    ungraded forever.
    Returns the number of settlements recorded.
    """
    unsettled = ctx.store.unsettled_calls()
    if not unsettled:
        return 0

    # Group by market_id for batch fetching
    by_market: dict[str, list[tuple[str, str, str]]] = {}
    for call_id, market_id, side, outcome_key in unsettled:
        by_market.setdefault(market_id, []).append((call_id, side, outcome_key))

    n = 0
    now = datetime.now(UTC)

    # Find the Kalshi exchange
    kalshi = None
    for ex in ctx.exchanges:
        if str(ex.venue_id) == "kalshi":
            kalshi = ex
            break
    if kalshi is None or not hasattr(kalshi, "market_results"):
        return 0

    tickers = list(by_market.keys())
    try:
        results = await kalshi.market_results(tickers)
    except Exception:
        log.exception("settlement check failed")
        return 0

    for ticker, result in results.items():
        if result is None:
            continue  # Not settled yet
        for call_id, side, outcome_key in by_market[ticker]:
            # Handle voided/cancelled markets: record as "void" so the call
            # doesn't get re-polled forever. Any result other than yes/no
            # is treated as void.
            if result not in ("yes", "no"):
                outcome = "void"
            elif (result == "yes" and side == "yes") or (result == "no" and side == "no"):
                outcome = "win"
            else:
                outcome = "loss"
            try:
                settlement = Settlement(
                    outcome_key=outcome_key,
                    result=outcome,
                    settled_at=now,
                )
                ctx.store.record_settlement(settlement)
                n += 1
            except Exception:
                log.exception("record_settlement failed for %s", call_id)

    return n


def grade_pending_calls(ctx: ServiceContext) -> int:
    """ADR-0008: grade settled calls as a separate, retryable per-loop step.

    Runs every loop over calls that have a settlement, have no grade at
    CALL_GRADE_METHOD_VERSION, and have a closing line at
    CLOSING_LINE_METHOD_VERSION. It only picks up ungraded calls, so
    re-running is always safe; the unique index on (call_id, method_version)
    stays as a backstop behind it.

    A transient grading error is caught per call and retried on the next
    loop instead of losing the call forever. A settled call with no closing
    line is skipped (not graded with CLV None): the close may be captured
    late, and until then the call shows up in the exclusion breakdown as
    "no close" rather than silently dropping out of the CLV numbers.

    Returns the number of grades written.
    """
    from .calls import (
        CALL_GRADE_METHOD_VERSION,
        CLOSING_LINE_METHOD_VERSION,
        Settlement,
    )

    now = datetime.now(UTC)
    n = 0
    for s in ctx.store.settled_calls():
        call_id = s["call_id"]
        if ctx.store.get_call_grade_version(call_id, CALL_GRADE_METHOD_VERSION) is not None:
            continue
        canonical = ctx.store.canonical_outcome_key(s["outcome"])
        if ctx.store.get_closing_line_version(canonical, CLOSING_LINE_METHOD_VERSION) is None:
            continue
        try:
            settlement = Settlement(
                outcome_key=canonical,
                result=s["result"],
                settled_at=datetime.fromisoformat(s["settled_at"]),
            )
            _grade_settled_call(
                ctx.store,
                call_id,
                canonical,
                settlement,
                now,
                close_version=CLOSING_LINE_METHOD_VERSION,
            )
            n += 1
        except Exception:
            log.exception("grading failed for %s; will retry next loop", call_id)
    return n


def _grade_settled_call(
    store: ObservationStore,
    call_id: str,
    outcome_key: str,
    settlement: Settlement,
    at: datetime,
    close_version: int | None = None,
) -> None:
    """Grade a call that just settled using the tested grade_call().

    Reconstructs PaperFill, ManualFill, CallSnapshot, and ClosingLine from
    the store, then calls grade_call() which handles fees, per-contract
    units, no-fill results, and edge half-life.

    The outcome key is canonicalized through the key-remap table before
    the closing-line lookup, so calls stored under an older normalization
    (e.g. MLB "margin <= 0") still join to their recomputed close.

    close_version pins the closing-line method version: None takes the
    latest version present (production), while the recompute migration
    pins the current version so a v2 grade never silently mixes in a v1
    close (it grades P&L-only instead).
    """
    from decimal import Decimal
    from typing import cast

    from mindgod.domain.propositions import Outcome
    from mindgod.domain.venues import ListingKey

    from .calls import (
        Call,
        CallSnapshot,
        ClosingLine,
        ManualFill,
        PaperFill,
        grade_call,
    )

    # Fetch call data (for ask_at_alert, limit_price_x, etc.)
    call_data = store.get_call(call_id)
    if not call_data:
        return

    # Construct minimal Call: grade_call only uses call_id, ask_at_alert,
    # limit_price_x. The listing_key and outcome are not used in grading.
    call = Call(
        call_id=call_data["call_id"],
        created_at=datetime.fromisoformat(call_data["created_at"]),
        listing_key=cast(ListingKey, None),
        outcome=cast(Outcome, None),
        fair_prob=call_data["fair_prob"],
        fair_se=call_data["fair_se"],
        fair_method=call_data["fair_method"],
        limit_price_x=Decimal(str(call_data["limit_price_x"])),
        contracts_n=call_data["contracts_n"],
        ask_at_alert=Decimal(str(call_data["ask_at_alert"])),
        net_edge_at_alert=Decimal(str(call_data["net_edge_at_alert"])),
        depth_at_x=call_data["depth_at_x"],
        event_start=datetime.fromisoformat(call_data["event_start"])
        if call_data["event_start"]
        else None,
    )

    # Fetch reaction fill (kind='reaction' specifically, per ADR-0008)
    reaction_fill = None
    fill_data = store.get_paper_fill(call_id)
    if fill_data:
        # ADR-0008: a "no fill" is itself a result; grade_call handles filled=False
        reaction_fill = PaperFill(
            call_id=fill_data["call_id"],
            kind=fill_data["kind"],
            contracts=fill_data["contracts"],
            avg_price=Decimal(str(fill_data["avg_price"]))
            if fill_data["avg_price"] is not None
            else None,
            fee=Decimal(str(fill_data["fee"])) if fill_data["fee"] is not None else None,
            filled=fill_data["filled"],
            at=datetime.fromisoformat(fill_data["at"]),
        )

    # Fetch manual fill (if any)
    manual_fill = None
    m_data = store.get_manual_fill(call_id)
    if m_data:
        manual_fill = ManualFill(
            call_id=m_data["call_id"],
            contracts=m_data["contracts"],
            price=Decimal(str(m_data["price"])),
            fee=Decimal(str(m_data["fee"])),
            taken_at=datetime.fromisoformat(m_data["taken_at"]),
            note=m_data["note"],
        )

    # Fetch snapshots
    snapshots = []
    for r in store.get_snapshots(call_id):
        snapshots.append(
            CallSnapshot(
                call_id=r["call_id"],
                offset_s=r["offset_s"],
                best_ask=Decimal(str(r["best_ask"])) if r["best_ask"] is not None else None,
                best_bid=Decimal(str(r["best_bid"])) if r["best_bid"] is not None else None,
                depth_at_x=r["depth_at_x"],
                recorded_at=datetime.fromisoformat(r["recorded_at"]),
            )
        )

    # Fetch closing line, canonicalizing the outcome key through the
    # remap table so older normalizations still join to their close.
    # close_version=None takes the latest version present; a pinned
    # version takes that version or nothing (never a silent mix).
    closing_line = None
    canonical_key = store.canonical_outcome_key(outcome_key)
    if close_version is None:
        cl_data = store.get_closing_line(canonical_key)
    else:
        cl_data = store.get_closing_line_version(canonical_key, close_version)
    if cl_data:
        closing_line = ClosingLine(
            outcome_key=cl_data["outcome_key"],
            sharp_close_prob=cl_data["sharp_close_prob"],
            source=cl_data["source"],
            captured_at=datetime.fromisoformat(cl_data["captured_at"]),
        )

    grade = grade_call(call, reaction_fill, manual_fill, snapshots, closing_line, settlement, at)
    store.record_call_grade(grade)


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
                    # ADR-0009: clear pitcher suppression only when BOTH conditions hold:
                    # the refreshed moneyline quote for that event has valid_at
                    # later than the change time, AND the 10-minute cool-off has
                    # elapsed. "Whichever is later" means both must be satisfied.
                    # This prevents lifting suppression before books have repriced.
                    if ctx.pitcher_suppressed:
                        now = datetime.now(UTC)
                        to_clear = []
                        for eid, change_time in ctx.pitcher_suppressed.items():
                            # Check cool-off: 10 minutes since change
                            cool_off_done = (now - change_time) >= timedelta(minutes=10)
                            # Check if the moneyline for this event has been repriced
                            # after the change time. Only moneyline quotes count;
                            # a nudge to the total must not clear moneyline suppression.
                            # We check the event_id; a more precise check would verify
                            # the market type, but the Quantity doesn't expose it directly.
                            repriced = False
                            for p in priced:
                                try:
                                    pid = str(p.outcome.quantity.event_id)
                                except AttributeError:
                                    continue
                                if pid == eid and p.quote.observed.valid_at > change_time:
                                    # valid_at is when the book last updated
                                    repriced = True
                                    break
                            # Both conditions must hold (AND, not OR)
                            if repriced and cool_off_done:
                                to_clear.append(eid)
                        for eid in to_clear:
                            del ctx.pitcher_suppressed[eid]
                        if to_clear:
                            log.info(
                                "cleared pitcher suppression for %d events",
                                len(to_clear),
                            )
                except Exception:
                    log.exception(
                        "sportsbook refresh failed; keeping %d stale prices",
                        len(priced),
                    )
                    _post_ops(ctx, "Odds API poll failed; keeping stale prices.")
                else:
                    _report_quota(ctx)
            opps = await tick(ctx, priced)
            if now_ts - last_discovery >= discovery_interval_s:
                await _discover(ctx)
                last_discovery = now_ts
            # ADR-0008: capture closing lines for events that just started.
            # Queries the quote log for pre-game consensus, not live prices.
            try:
                n_closed = capture_closing_lines(ctx, datetime.now(UTC))
                if n_closed:
                    log.info("captured %d closing lines", n_closed)
            except Exception:
                log.exception("closing line capture failed")
            # ADR-0008: check for settled markets and record settlements.
            try:
                n_settled = await _check_settlements(ctx)
                if n_settled:
                    log.info("recorded %d settlements", n_settled)
            except Exception:
                log.exception("settlement check failed")
            # ADR-0008: grade settled calls as a separate retryable step.
            # Only picks up calls with a settlement, no current-version
            # grade, and a closing line; safe to run every loop.
            try:
                n_graded = grade_pending_calls(ctx)
                if n_graded:
                    log.info("graded %d settled calls", n_graded)
            except Exception:
                log.exception("grading step failed")
            log.info("tick done: %d opportunities", len(opps))
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(exchange_interval_s)
