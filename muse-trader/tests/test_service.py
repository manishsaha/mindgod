"""Terms-carrying fair values: the KC -3 push regression and its guard rails.

A whole-number spread line refunds on a push; a half-point line does not.
The same canonical outcome with different refund terms is a different bet,
so the service only uses a fair value whose source terms match the
listing's exactly. These tests pin that rule end to end.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from mindgod.adapters.config import ListingSpec
from mindgod.adapters.listings import build_event, build_listing, event_id_for
from mindgod.adapters.odds_api import OddsApiSource
from mindgod.application.opportunities import Opportunity
from mindgod.application.ports import ListingKey, PricedOutcome
from mindgod.application.pricing import partition_by_terms, terms_key
from mindgod.application.service import ServiceContext, tick
from mindgod.domain.primitives import ContractPrice, Observation, Probability
from mindgod.domain.propositions import margin_exactly, spread
from mindgod.domain.quotes import FillEstimate, OrderBook, PriceLevel, SportsbookQuote
from mindgod.domain.sports import Event, League, TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import VenueId

NOW = datetime(2026, 9, 25, tzinfo=UTC)
KALSHI = VenueId("kalshi")
DK = VenueId("draftkings")
KC = TeamId("nfl-kc")
BUF = TeamId("nfl-buf")


def _spec(**over) -> ListingSpec:
    kw: dict = dict(
        venue="kalshi",
        market_id="KXNFLGAME-26OCT05KCBUF",
        side="yes",
        league="nfl",
        home="KC",
        away="BUF",
        start="2026-10-05T17:00:00Z",
        outcome_kind="spread",
        outcome_team="home",
        handicap="-3",
    )
    kw.update(over)
    return ListingSpec(**kw)


def _terms(outcome, refunds):
    return Terms(Payoff(outcome, refunds))


def _feed_priced(outcome, terms, odds) -> PricedOutcome:
    key = ListingKey(DK, "dk-game", "spreads:KC")
    return PricedOutcome(
        outcome=outcome,
        listing_key=key,
        quote=SportsbookQuote(key, odds, Observation(NOW, NOW)),
        market_group="draftkings:dk-game:spreads",
        terms=terms,
    )


def _book(key) -> OrderBook:
    return OrderBook(
        listing=key,
        asks=(PriceLevel(ContractPrice(Decimal("0.47")), 10),),
        bids=(),
        observed=Observation(NOW, NOW),
    )


class FakeExchange:
    venue_id = KALSHI

    def __init__(self, books):
        self._books = books

    async def order_books(self, listings):
        return self._books

    async def discover(self):
        return []


class FakeResolver:
    review_queue: list

    def __init__(self, listing, event):
        self._listing = listing
        self._starts = {listing.key: event.scheduled_start}
        self.review_queue = []

    def register(self, listing): ...
    def resolve(self, key):
        return self._listing

    def listings_for(self, venue_id):
        return [self._listing]

    def report_unmapped(self, market): ...

    def event_start(self, key):
        return self._starts.get(key)


class FakeModel:
    def __init__(self, by_terms):
        self.by_terms = by_terms

    def values_by_terms(self, priced, as_of):
        return self.by_terms


class FakeDetector:
    def __init__(self):
        self.seen: list = []

    def detect(self, books, fair):
        self.seen.append(fair)
        return []


class FakeRisk:
    def approve(self, opp):
        return True

    def record_fill(self, fill): ...


class FakeStore:
    def record_priced(self, priced, now): ...
    def record_books(self, books, now): ...
    def record_opportunity(self, opportunity, at): ...
    def record_fill(self, fill): ...


def _listing_and_event():
    spec = _spec()
    return build_listing(spec), build_event(spec)


def _context(listing, event, books, model, detector=None) -> ServiceContext:
    return ServiceContext(
        sportsbook=None,
        exchanges=[FakeExchange(books)],
        execution={},
        resolver=FakeResolver(listing, event),
        model=model,
        detector=detector or FakeDetector(),
        risk=FakeRisk(),
        store=FakeStore(),
        horizon_days=30,  # the fixture event is Oct 5, ten days out
    )


def test_no_fair_value_when_terms_differ():
    """Only half-point prices exist; the listing refunds pushes. Nothing may
    trade: the detector never even runs."""
    listing, event = _listing_and_event()
    outcome = spread(event, KC, Decimal("-3"))
    priced = [_feed_priced(outcome, _terms(outcome, None), -110)]
    book = _book(listing.key)
    ctx = _context(listing, event, [book], FakeModel({}))
    opps = asyncio.new_event_loop().run_until_complete(tick(ctx, priced))
    assert opps == []
    assert ctx.detector.seen == []


def test_matching_terms_flow_through():
    """The same terms on both sides: the fair value reaches the detector and
    an opportunity is recorded."""
    listing, event = _listing_and_event()
    outcome = spread(event, KC, Decimal("-3"))
    push = margin_exactly(event, KC, Decimal("3"))
    priced = [_feed_priced(outcome, _terms(outcome, push), -110)]
    fair = FairValue(
        outcome=outcome,
        probability=Probability(Decimal("0.5")),
        standard_error=0.0,
        as_of=NOW,
        method="test",
        sources=(),
    )

    class DetectingDetector:
        def __init__(self):
            self.seen: list = []

        def detect(self, books, fair_values):
            self.seen.append(fair_values)
            opp = Opportunity(
                listing=books[0][0].key,
                outcome=outcome,
                fair_value=fair_values[outcome],
                fill=FillEstimate(
                    contracts=1,
                    notional=Decimal("0.47"),
                    worst_price=ContractPrice(Decimal("0.47")),
                ),
                fee=Decimal("0.01"),
                edge_net=Decimal("0.02"),
                stake=Decimal("0.47"),
            )
            return [opp]

    detector = DetectingDetector()
    key = terms_key(Terms(Payoff(outcome, push)))
    ctx = _context(
        listing, event, [_book(listing.key)], FakeModel({key: {outcome: fair}}), detector
    )
    opps = asyncio.new_event_loop().run_until_complete(tick(ctx, priced))
    assert len(opps) == 1
    assert opps[0].fair_value.probability.value == Decimal("0.5")
    assert detector.seen[0] == {outcome: fair}


def test_partition_by_terms_keeps_push_lines_apart():
    listing, event = _listing_and_event()
    outcome = spread(event, KC, Decimal("-3"))
    push = _feed_priced(
        outcome,
        _terms(outcome, margin_exactly(event, KC, Decimal("3"))),
        -110,
    )
    nopush = _feed_priced(outcome, _terms(outcome, None), -110)
    partitions = partition_by_terms([push, nopush])
    assert len(partitions) == 2


def test_odds_api_whole_number_spread_refunds_push():
    source = OddsApiSource("key")
    event = Event(
        id=event_id_for(League.NFL, "KC", "BUF", "2026-10-05"),
        league=League.NFL,
        home=KC,
        away=BUF,
        scheduled_start=NOW,
    )
    whole = {
        "key": "spreads",
        "last_update": "2026-09-25T10:00:00Z",
        "outcomes": [
            {"name": "Kansas City Chiefs", "price": -110, "point": -3},
            {"name": "Buffalo Bills", "price": -110, "point": 3},
        ],
    }
    parsed = source._parse_market(League.NFL, event, DK, {"key": "draftkings"}, whole, NOW)
    assert len(parsed) == 2
    assert parsed[0].terms.payoff.refunds_if is not None
    assert parsed[0].quote.observed.valid_at == datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    # The underdog's push is the same game, not a mirrored one: BUF +3
    # refunds exactly when KC wins by 3, so both sides share one terms key
    # and devig together instead of passing the vig through as lone sides.
    fav_push = parsed[0].terms.payoff.refunds_if
    dog_push = parsed[1].terms.payoff.refunds_if
    assert dog_push is not None
    assert dog_push == fav_push
    assert dog_push == margin_exactly(event, KC, Decimal("3"))
    assert terms_key(parsed[0].terms) == terms_key(parsed[1].terms)

    half = {
        "key": "spreads",
        "last_update": "2026-09-25T10:00:00Z",
        "outcomes": [
            {"name": "Kansas City Chiefs", "price": -110, "point": -3.5},
            {"name": "Buffalo Bills", "price": -110, "point": 3.5},
        ],
    }
    parsed = source._parse_market(League.NFL, event, DK, {"key": "draftkings"}, half, NOW)
    assert parsed[0].terms.payoff.refunds_if is None


def _book_at(key, price: str) -> OrderBook:
    return OrderBook(
        listing=key,
        asks=(PriceLevel(ContractPrice(Decimal(price)), 10),),
        bids=(),
        observed=Observation(NOW, NOW),
    )


def _fair_for(listing, event):
    outcome = spread(event, KC, Decimal("-3"))
    push = margin_exactly(event, KC, Decimal("3"))
    priced = [_feed_priced(outcome, _terms(outcome, push), -110)]
    fair = FairValue(
        outcome=outcome,
        probability=Probability(Decimal("0.5")),
        standard_error=0.0,
        as_of=NOW,
        method="test",
        sources=(),
    )
    return priced, {terms_key(Terms(Payoff(outcome, push))): {outcome: fair}}


def test_move_check_suppresses_stale_consensus():
    """The Kalshi mid moved 5c since the sportsbook refresh: the consensus
    is stale, so the detector never runs and nothing is recorded."""
    listing, event = _listing_and_event()
    priced, by_terms = _fair_for(listing, event)
    ctx = _context(listing, event, [_book_at(listing.key, "0.52")], FakeModel(by_terms))
    ctx.mid_at_refresh[listing.key] = 0.47
    opps = asyncio.new_event_loop().run_until_complete(tick(ctx, priced))
    assert opps == []
    assert ctx.detector.seen == []


def test_small_move_does_not_suppress():
    listing, event = _listing_and_event()
    priced, by_terms = _fair_for(listing, event)
    ctx = _context(listing, event, [_book_at(listing.key, "0.48")], FakeModel(by_terms))
    ctx.mid_at_refresh[listing.key] = 0.47
    asyncio.new_event_loop().run_until_complete(tick(ctx, priced))
    assert ctx.detector.seen != []


def test_refresh_flag_snapshots_mids():
    listing, event = _listing_and_event()
    priced, by_terms = _fair_for(listing, event)
    ctx = _context(listing, event, [_book_at(listing.key, "0.47")], FakeModel(by_terms))
    ctx.sportsbook_refreshed = True
    asyncio.new_event_loop().run_until_complete(tick(ctx, priced))
    assert ctx.mid_at_refresh[listing.key] == 0.47
    assert ctx.sportsbook_refreshed is False


class _RecordingNotifier:
    def __init__(self):
        self.sent: list = []

    async def send(self, opp) -> bool:
        self.sent.append(opp)
        return True


class _PaperExecution:
    async def buy(self, opp):
        return None


def test_notification_task_is_held_until_it_runs():
    """A fire-and-forget create_task with no reference can be garbage
    collected before the send runs; the context retains every task."""
    listing, event = _listing_and_event()
    priced, by_terms = _fair_for(listing, event)
    outcome = spread(event, KC, Decimal("-3"))

    class DetectingDetector:
        def detect(self, books, fair_values):
            return [
                Opportunity(
                    listing=books[0][0].key,
                    outcome=outcome,
                    fair_value=fair_values[outcome],
                    fill=FillEstimate(
                        contracts=1,
                        notional=Decimal("0.47"),
                        worst_price=ContractPrice(Decimal("0.47")),
                    ),
                    fee=Decimal("0.01"),
                    edge_net=Decimal("0.02"),
                    stake=Decimal("0.47"),
                )
            ]

    notifier = _RecordingNotifier()
    ctx = _context(listing, event, [_book(listing.key)], FakeModel(by_terms), DetectingDetector())
    ctx.execution = {KALSHI: _PaperExecution()}
    ctx.notifier = notifier

    async def _main():
        opps = await tick(ctx, priced)
        for _ in range(10):
            await asyncio.sleep(0)
        return opps

    opps = asyncio.new_event_loop().run_until_complete(_main())
    assert len(opps) == 1
    assert len(notifier.sent) == 1
    assert ctx.notify_tasks == set()


def test_mlb_moneyline_skipped_for_listed_pitchers():
    """Neither feed reports listed pitchers, so an MLB moneyline's terms can
    never be proven equal. The market is skipped loudly, not priced."""
    source = OddsApiSource("key")
    event = Event(
        id=event_id_for(League.MLB, "NYY", "BOS", "2026-10-05"),
        league=League.MLB,
        home=TeamId("mlb-nyy"),
        away=TeamId("mlb-bos"),
        scheduled_start=NOW,
    )
    market = {
        "key": "h2h",
        "last_update": "2026-09-25T10:00:00Z",
        "outcomes": [
            {"name": "New York Yankees", "price": -150},
            {"name": "Boston Red Sox", "price": 130},
        ],
    }
    parsed = source._parse_market(League.MLB, event, DK, {"key": "draftkings"}, market, NOW)
    assert parsed == []
