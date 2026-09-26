"""Closing-line capture and the full alert-to-grade loop.

test_closing_lines_devigged_not_vig_included exercises the real
capture_closing_lines on a lopsided -150/+130 NFL spread pair and asserts
the devigged close (not the vig-included average), that Yes and No get
different values summing to 1 (catches side swaps and double flips), and
that a whole-number pair fails closed with no closing line.

test_full_cycle_settlement_and_grade runs the production path end to end:
capture_closing_lines, then _check_settlements with a fake Kalshi result,
then asserts the persisted grade's CLV equals
close - reaction fill price - fee per contract.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import mindgod.application.calls as calls_mod
from mindgod.adapters.store import Store
from mindgod.application.calls import (
    CALL_GRADE_METHOD_VERSION,
    Call,
    PaperFill,
    Settlement,
)
from mindgod.application.ports import ListingKey, PricedOutcome
from mindgod.application.pricing import WeightedConsensusModel, devig, outcome_key
from mindgod.application.service import (
    ServiceContext,
    _check_settlements,
    capture_closing_lines,
    grade_pending_calls,
)
from mindgod.domain.primitives import Observation, Probability
from mindgod.domain.propositions import spread
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, EventId, League, TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import VenueId

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
KALSHI = VenueId("kalshi")
DK = VenueId("draftkings")
FD = VenueId("fanduel")
KC = TeamId("nfl-kc")
BUF = TeamId("nfl-buf")


class FakeExchange:
    """Minimal exchange for testing."""

    def __init__(self, venue_id, listings):
        self.venue_id = venue_id
        self._listings = listings
        self._results: dict[str, str] = {}

    async def discover(self):
        return []

    async def order_book(self, market_id):
        return None

    async def market_results(self, tickers):
        """Fake Kalshi settlement results, keyed by market ticker."""
        return {t: self._results.get(t) for t in tickers}


class FakeResolver:
    """Resolver that returns our test listings."""

    def __init__(self, listings, event):
        self._listings = listings
        self._event = event
        self.review_queue = []

    def listings_for(self, venue_id):
        return [listing for listing in self._listings if listing.key.venue_id == venue_id]

    def event_start(self, key):
        # All listings are for the same event
        return self._event.scheduled_start

    def resolve(self, key):
        for listing in self._listings:
            if listing.key == key:
                return listing
        return None

    def register(self, listing): ...
    def report_unmapped(self, market): ...


class FakeListing:
    """Minimal listing with outcome, key, and terms."""

    def __init__(self, key, outcome, terms):
        self.key = key
        self.outcome = outcome
        self.terms = terms


def _make_event(suffix=""):
    return Event(
        id=EventId(f"nfl-kc-buf-20261005{suffix}"),
        league=League.NFL,
        home=KC,
        away=BUF,
        scheduled_start=datetime(2026, 10, 5, 17, 0, 0, tzinfo=UTC),
    )


def _make_terms(outcome):
    """Standard terms: no push refund for half-point, push refund for whole."""
    return Terms(
        payoff=Payoff(outcome, refunds_if=None),
        void_policy="refund",
    )


def _record_half_point_quotes(store, event_hp, outcome_hp_yes, outcome_hp_no):
    """Record lopsided -150/+130 quotes from two books for the half-point pair.

    valid_at is 2 hours before kickoff (the line held steady) but the feed
    kept confirming it 15 minutes before kickoff. Per ADR-0007, staleness
    gates on confirmation age (recorded_at), not on when the price last
    moved.
    """
    valid_at = event_hp.scheduled_start - timedelta(hours=2)
    recorded_at = event_hp.scheduled_start - timedelta(minutes=15)
    priced = []
    for venue in [DK, FD]:
        for outcome, odds in [(outcome_hp_yes, -150), (outcome_hp_no, 130)]:
            key = ListingKey(venue_id=venue, market_id="test", side="yes")
            obs = Observation(valid_at=valid_at, recorded_at=recorded_at)
            quote = SportsbookQuote(listing=key, american_odds=odds, observed=obs)
            priced.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=key,
                    quote=quote,
                    market_group=f"{venue}:test",
                    terms=_make_terms(outcome),
                )
            )
    store.record_priced(priced, recorded_at)


def _make_ctx(store, listings, event_hp):
    """Build a ServiceContext with the real model and fake venue adapters."""
    model = WeightedConsensusModel(
        method="power",
        book_weights={str(DK): 2.0, str(FD): 1.0},  # DK is sharper
        max_quote_age_s=3600,  # 1 hour
    )
    resolver = FakeResolver(listings, event_hp)
    return ServiceContext(
        sportsbook=None,
        exchanges=[FakeExchange(KALSHI, listings)],
        execution={},
        resolver=resolver,
        model=model,
        detector=None,  # type: ignore
        risk=None,  # type: ignore
        store=store,
    )


def test_closing_lines_devigged_not_vig_included():
    """Lopsided -150/+130 must devig; symmetric data can't catch a side swap.

    Sets up:
    - KC -3.5 (Yes/No listings) with -150/+130 quotes from two books
    - KC -3 / BUF +3 (whole number) with -110/-110 quotes

    Asserts:
    - Yes and No closing lines are the devigged values, different from each
      other and summing to 1. A bug that swaps the sides (or flips twice)
      fails the ordering assertion.
    - The whole-number pair produces NO closing line (complement doesn't
      pair exactly: "margin >= 4" vs "margin <= 2").
    """
    store = Store(":memory:")
    event_hp = _make_event("-hp")
    event_wn = _make_event("-wn")

    # Half-point spread: KC -3.5
    outcome_hp_yes = spread(event_hp, KC, Decimal("-3.5"))
    outcome_hp_no = outcome_hp_yes.complement()
    assert outcome_hp_no is not None

    # Whole-number spread: KC -3 (push refunds)
    # In reality, books quote KC -3 and BUF +3 as separate outcomes:
    # - KC -3 Yes: "margin >= 4" (KC wins by 4+)
    # - BUF +3 Yes: "margin <= 2" (BUF loses by 2 or less, i.e., KC wins by <=2)
    # These are NOT exact complements (complement of >=4 is <=3, not <=2).
    # The push (margin == 3) is missing from both, so they don't pair.
    outcome_wn_kc = spread(event_wn, KC, Decimal("-3"))  # margin >= 4
    outcome_wn_buf = spread(event_wn, BUF, Decimal("+3"))  # margin <= 2 (for BUF side)
    # Note: outcome_wn_buf is NOT the complement of outcome_wn_kc!

    # Create listings
    listings = []
    # Half-point pair: Yes and No are exact complements
    for outcome, side in [
        (outcome_hp_yes, "yes"),
        (outcome_hp_no, "no"),
    ]:
        key = ListingKey(
            venue_id=KALSHI,
            market_id="KXNFLGAME-26OCT05KCBUF",
            side=side,
        )
        terms = _make_terms(outcome)
        listings.append(FakeListing(key, outcome, terms))
    # Whole-number pair: KC -3 and BUF +3 are NOT complements
    for outcome, side in [
        (outcome_wn_kc, "yes"),
        (outcome_wn_buf, "yes"),
    ]:
        key = ListingKey(
            venue_id=KALSHI,
            market_id="KXNFLGAME-26OCT05KCBUF-WN",
            side=side,
        )
        terms = _make_terms(outcome)
        listings.append(FakeListing(key, outcome, terms))

    # Lopsided -150/+130 quotes for the half-point pair (see helper docstring
    # for why valid_at is old while recorded_at is fresh).
    _record_half_point_quotes(store, event_hp, outcome_hp_yes, outcome_hp_no)

    # Whole-number pair: NOT complements, will NOT pair
    valid_at = event_hp.scheduled_start - timedelta(hours=2)
    recorded_at = event_hp.scheduled_start - timedelta(minutes=15)
    priced = []
    for venue in [DK, FD]:
        for outcome in [outcome_wn_kc, outcome_wn_buf]:
            key = ListingKey(venue_id=venue, market_id="test-wn", side="yes")
            obs = Observation(valid_at=valid_at, recorded_at=recorded_at)
            quote = SportsbookQuote(
                listing=key,
                american_odds=-110,
                observed=obs,
            )
            priced.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=key,
                    quote=quote,
                    market_group=f"{venue}:test-wn",
                    terms=_make_terms(outcome),
                )
            )
    store.record_priced(priced, recorded_at)

    # Build context
    ctx = _make_ctx(store, listings, event_hp)

    # Capture closing lines (event started 30 min ago)
    at = event_hp.scheduled_start + timedelta(minutes=30)
    n = capture_closing_lines(ctx, at)

    # Should capture 2: Yes and No for half-point pair
    # Whole-number pair should NOT be captured (complement doesn't pair)
    assert n == 2, f"Expected 2 closing lines, got {n}"

    # Check the half-point pair: both should be ~0.500 (devigged)
    hp_yes_key = outcome_key(outcome_hp_yes)
    hp_no_key = outcome_key(outcome_hp_no)

    cl_yes = store.get_closing_line(hp_yes_key)
    cl_no = store.get_closing_line(hp_no_key)

    assert cl_yes is not None, "Half-point Yes closing line missing"
    assert cl_no is not None, "Half-point No closing line missing"

    # Check the half-point pair: devigged values from the production path.
    # -150 => 0.60, +130 => 0.4348; power-devig removes the 3.5% overround.
    # Both books quote the same prices, so sharp weights don't shift the mean.
    hp_yes_key = outcome_key(outcome_hp_yes)
    hp_no_key = outcome_key(outcome_hp_no)

    cl_yes = store.get_closing_line(hp_yes_key)
    cl_no = store.get_closing_line(hp_no_key)

    assert cl_yes is not None, "Half-point Yes closing line missing"
    assert cl_no is not None, "Half-point No closing line missing"

    p_yes = Probability.from_american_odds(-150).value
    p_no = Probability.from_american_odds(130).value
    expected_yes, expected_no = devig([p_yes, p_no], "power")

    yes_close = cl_yes["sharp_close_prob"]
    no_close = cl_no["sharp_close_prob"]
    assert yes_close == pytest.approx(expected_yes, abs=1e-6), (
        f"Yes close {yes_close} != devigged {expected_yes}"
    )
    assert no_close == pytest.approx(expected_no, abs=1e-6), (
        f"No close {no_close} != devigged {expected_no}"
    )
    # Lopsidedness is the point: Yes and No must differ and sum to 1.
    # A side swap (or the old double flip) would put the small value on Yes.
    assert yes_close > no_close, f"sides look swapped: Yes {yes_close} <= No {no_close}"
    assert yes_close + no_close == pytest.approx(1.0, abs=1e-6)

    # Whole-number pair should have NO closing line
    # (KC -3 and BUF +3 are not exact complements, so they don't pair)
    wn_kc_key = outcome_key(outcome_wn_kc)
    wn_buf_key = outcome_key(outcome_wn_buf)
    assert store.get_closing_line(wn_kc_key) is None, (
        "Whole-number KC -3 should not have a closing line (complement doesn't pair)"
    )
    assert store.get_closing_line(wn_buf_key) is None, (
        "Whole-number BUF +3 should not have a closing line (complement doesn't pair)"
    )


def test_full_cycle_settlement_and_grade():
    """Alert -> reaction fill -> close -> settlement -> persisted grade.

    Uses the real production path throughout: the real capture_closing_lines
    (lopsided -150/+130 quotes, so the close is not symmetric), the real
    _check_settlements against a fake Kalshi exchange returning a batched
    result (which must NOT grade as a side effect), then the real
    grade_pending_calls step, and the real _grade_settled_call via the tested
    grade_call().

    Asserts the persisted grade's CLV equals
    close - reaction fill price - fee per contract, and that the instant
    fill is ignored (ADR-0008: only the reaction fill feeds headline metrics).
    """
    store = Store(":memory:")
    event_hp = _make_event("-hp")
    outcome_hp_yes = spread(event_hp, KC, Decimal("-3.5"))
    outcome_hp_no = outcome_hp_yes.complement()
    assert outcome_hp_no is not None

    listings = []
    for outcome, side in [(outcome_hp_yes, "yes"), (outcome_hp_no, "no")]:
        key = ListingKey(
            venue_id=KALSHI,
            market_id="KXNFLGAME-26OCT05KCBUF",
            side=side,
        )
        listings.append(FakeListing(key, outcome, _make_terms(outcome)))

    _record_half_point_quotes(store, event_hp, outcome_hp_yes, outcome_hp_no)
    ctx = _make_ctx(store, listings, event_hp)

    # The alert fired on the Yes listing; record the call.
    alert_at = event_hp.scheduled_start - timedelta(hours=3)
    call = Call(
        call_id="call-1",
        created_at=alert_at,
        listing_key=listings[0].key,
        outcome=outcome_hp_yes,
        fair_prob=0.58,
        fair_se=0.02,
        fair_method="power-devig",
        limit_price_x=Decimal("0.60"),
        contracts_n=10,
        ask_at_alert=Decimal("0.57"),
        net_edge_at_alert=Decimal("0.01"),
        depth_at_x=100,
        event_start=event_hp.scheduled_start,
    )
    store.record_call(call)

    # Two fills: an instant fill (must be ignored for grading) and the
    # reaction-delay fill the service actually records after the alert.
    fill_at = alert_at + timedelta(seconds=10)
    store.record_paper_fill(
        PaperFill(
            call_id="call-1",
            kind="instant",
            contracts=10,
            avg_price=Decimal("0.50"),
            fee=Decimal("0.10"),
            filled=True,
            at=fill_at,
        )
    )
    store.record_paper_fill(
        PaperFill(
            call_id="call-1",
            kind="reaction",
            contracts=10,
            avg_price=Decimal("0.55"),
            fee=Decimal("0.10"),
            filled=True,
            at=fill_at + timedelta(seconds=45),
        )
    )

    # Close: real capture after the event started.
    at = event_hp.scheduled_start + timedelta(minutes=30)
    n = capture_closing_lines(ctx, at)
    assert n == 2, f"Expected 2 closing lines, got {n}"
    close = store.get_closing_line(outcome_key(outcome_hp_yes))
    assert close is not None
    close_prob = close["sharp_close_prob"]

    # Settlement: fake Kalshi returns a batched "yes" result for the market.
    # Settlement records the fact only; it must NOT grade as a side effect.
    exchange = ctx.exchanges[0]
    assert isinstance(exchange, FakeExchange)
    exchange._results["KXNFLGAME-26OCT05KCBUF"] = "yes"
    n_settled = asyncio.run(_check_settlements(ctx))
    assert n_settled == 1, f"Expected 1 settlement, got {n_settled}"
    assert store.get_call_grade("call-1") is None, "settlement must not grade as a side effect"

    # Grading is a separate per-loop step: it picks up the settled call now
    # that a closing line exists.
    n_graded = grade_pending_calls(ctx)
    assert n_graded == 1, f"Expected 1 grade, got {n_graded}"

    # The persisted grade must tie CLV to the close, the REACTION fill price
    # (0.55, not the instant fill's 0.50), and the fee per contract
    # (0.10 total / 10 contracts = 0.01).
    grade = store.get_call_grade("call-1")
    assert grade is not None, "no grade persisted for the settled call"
    expected_clv = close_prob - 0.55 - 0.01
    assert grade["clv_reaction"] == pytest.approx(expected_clv, abs=1e-9), (
        f"CLV {grade['clv_reaction']} != close {close_prob} - 0.55 - 0.01"
    )
    # The call won (side yes, result yes): P&L per contract is (1 - price) - fee.
    assert grade["pnl_reaction"] == pytest.approx((1.0 - 0.55) - 0.01, abs=1e-9)


def _record_settled_call(store, call_id, outcome, event, at):
    """Record a call, its reaction fill, and a winning settlement."""
    key = ListingKey(venue_id=KALSHI, market_id="KX-TEST", side="yes")
    call = Call(
        call_id=call_id,
        created_at=at,
        listing_key=key,
        outcome=outcome,
        fair_prob=0.58,
        fair_se=0.02,
        fair_method="power-devig",
        limit_price_x=Decimal("0.60"),
        contracts_n=10,
        ask_at_alert=Decimal("0.57"),
        net_edge_at_alert=Decimal("0.01"),
        depth_at_x=100,
        event_start=event.scheduled_start,
    )
    store.record_call(call)
    store.record_paper_fill(
        PaperFill(
            call_id=call_id,
            kind="reaction",
            contracts=10,
            avg_price=Decimal("0.55"),
            fee=Decimal("0.10"),
            filled=True,
            at=at + timedelta(seconds=45),
        )
    )
    store.record_settlement(
        Settlement(
            outcome_key=outcome_key(outcome),
            result="win",
            settled_at=event.scheduled_start + timedelta(hours=4),
        )
    )


def test_grading_retries_after_transient_failure(monkeypatch):
    """A transient grading error must not lose the call forever.

    The old path graded as a side effect of settlement: one exception and
    the call never came back. The per-loop step catches per call and picks
    it up again on the next loop.
    """
    store = Store(":memory:")
    event = _make_event("-retry")
    outcome_yes = spread(event, KC, Decimal("-3.5"))
    outcome_no = outcome_yes.complement()
    assert outcome_no is not None
    _record_half_point_quotes(store, event, outcome_yes, outcome_no)
    listings = [
        FakeListing(
            ListingKey(venue_id=KALSHI, market_id="KX-R", side="yes"),
            outcome_yes,
            _make_terms(outcome_yes),
        )
    ]
    ctx = _make_ctx(store, listings, event)
    assert capture_closing_lines(ctx, event.scheduled_start + timedelta(minutes=30)) == 1
    alert_at = event.scheduled_start - timedelta(hours=3)
    _record_settled_call(store, "call-retry", outcome_yes, event, alert_at)

    real_grade_call = calls_mod.grade_call
    attempts = 0

    def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient boom")
        return real_grade_call(*args, **kwargs)

    monkeypatch.setattr(calls_mod, "grade_call", flaky)

    assert grade_pending_calls(ctx) == 0
    assert store.get_call_grade_version("call-retry", CALL_GRADE_METHOD_VERSION) is None

    # Next loop: the same call is picked up again and graded.
    assert grade_pending_calls(ctx) == 1
    grade = store.get_call_grade_version("call-retry", CALL_GRADE_METHOD_VERSION)
    assert grade is not None
    assert grade["clv_reaction"] is not None

    # And grading is idempotent: a third loop writes nothing new.
    assert grade_pending_calls(ctx) == 0
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM call_grades WHERE call_id = 'call-retry'"
    ).fetchone()[0]
    assert rows == 1


def test_grading_skips_call_without_close_then_grades_after_late_capture():
    """A settled call with no closing line is left ungraded, not CLV-None.

    The close may be captured late (no one-hour window); once it exists the
    next grading loop picks the call up.
    """
    store = Store(":memory:")
    event = _make_event("-noclose")
    outcome_yes = spread(event, KC, Decimal("-3.5"))
    outcome_no = outcome_yes.complement()
    assert outcome_no is not None
    listings = [
        FakeListing(
            ListingKey(venue_id=KALSHI, market_id="KX-N", side="yes"),
            outcome_yes,
            _make_terms(outcome_yes),
        )
    ]
    ctx = _make_ctx(store, listings, event)
    alert_at = event.scheduled_start - timedelta(hours=3)
    _record_settled_call(store, "call-noclose", outcome_yes, event, alert_at)

    # No quotes recorded: no close can be captured, so no grade.
    assert grade_pending_calls(ctx) == 0
    assert store.get_call_grade_version("call-noclose", CALL_GRADE_METHOD_VERSION) is None

    # Quotes arrive late (after a restart, say). Capture has no one-hour
    # window: three hours after kickoff it still computes the same close
    # from stored pre-kickoff history.
    _record_half_point_quotes(store, event, outcome_yes, outcome_no)
    n = capture_closing_lines(ctx, event.scheduled_start + timedelta(hours=3))
    assert n == 1

    assert grade_pending_calls(ctx) == 1
    grade = store.get_call_grade_version("call-noclose", CALL_GRADE_METHOD_VERSION)
    assert grade is not None
    assert grade["clv_reaction"] is not None


def test_capture_skips_events_that_have_not_started():
    """Closing lines are pre-game consensus: future events are not captured."""
    store = Store(":memory:")
    event = _make_event("-future")
    outcome_yes = spread(event, KC, Decimal("-3.5"))
    outcome_no = outcome_yes.complement()
    assert outcome_no is not None
    _record_half_point_quotes(store, event, outcome_yes, outcome_no)
    listings = [
        FakeListing(
            ListingKey(venue_id=KALSHI, market_id="KX-F", side="yes"),
            outcome_yes,
            _make_terms(outcome_yes),
        )
    ]
    ctx = _make_ctx(store, listings, event)
    assert capture_closing_lines(ctx, event.scheduled_start - timedelta(hours=1)) == 0
    assert store.get_closing_line(outcome_key(outcome_yes)) is None
