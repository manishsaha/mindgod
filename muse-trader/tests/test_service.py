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
from mindgod.application.pricing import partition_by_terms
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
    def __init__(self, values):
        self.values = values

    def value(self, priced, as_of):
        return self.values


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
    ctx = _context(listing, event, [_book(listing.key)], FakeModel({outcome: fair}), detector)
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
