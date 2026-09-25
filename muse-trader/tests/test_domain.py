from datetime import datetime, timedelta, timezone

from edge_engine.domain import (
    CanonicalMarket,
    FairSource,
    MarketBook,
    MarketMapper,
    OrderBookLevel,
    Outcome,
    Quote,
    QuoteLog,
    SettlementRules,
    build_fair_value,
)

T0 = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)


def make_market(**over):
    kw = dict(
        market_id="nfl:KC-BUF:20261005:moneyline",
        event_id="nfl:KC-BUF:20261005",
        label="Chiefs vs Bills",
        market_type="moneyline",
        outcomes=[Outcome("home", "Chiefs"), Outcome("away", "Bills")],
        settlement=SettlementRules(source="kalshi"),
        starts_at=T0,
    )
    kw.update(over)
    return CanonicalMarket(**kw)


def test_settlement_fingerprint_detects_rule_changes():
    a = SettlementRules(source="kalshi")
    assert a.fingerprint() == SettlementRules(source="kalshi").fingerprint()
    assert a.fingerprint() != SettlementRules(
        source="kalshi", overtime_included=False).fingerprint()


def test_mapper_resolves_and_quarantines_rule_mismatch():
    mapper = MarketMapper()
    market = make_market()
    fp = market.settlement.fingerprint()
    mapper.register(market, {"kalshi": ("KXNFLGAME-26OCT05KCBUF", fp)})

    assert mapper.resolve("kalshi", "KXNFLGAME-26OCT05KCBUF", fp) is market
    assert mapper.resolve("kalshi", "UNKNOWN-TICKER", fp) is None

    changed = SettlementRules(source="kalshi",
                              overtime_included=False).fingerprint()
    assert mapper.resolve("kalshi", "KXNFLGAME-26OCT05KCBUF", changed) is None
    assert len(mapper.mismatches) == 1
    assert mapper.mismatches[0].canonical_market_id == market.market_id


def test_mapper_suggest_ranks_best_match_first():
    mapper = MarketMapper()
    target = make_market()
    other = make_market(market_id="nfl:KC-MIA:20261012:moneyline",
                        label="Chiefs vs Dolphins",
                        starts_at=T0 + timedelta(days=7))
    mapper.register(target, {})
    mapper.register(other, {})

    scored = mapper.suggest("Kansas City Chiefs vs Buffalo Bills", starts_at=T0)
    assert scored[0][0] is target
    assert scored[0][1] > scored[1][1]


def test_quote_log_is_bitemporal():
    log = QuoteLog(":memory:")

    def q(price, observed):
        return Quote(venue="kalshi", venue_market_id="T1", outcome_id="yes",
                     side="ask", price=price, canonical_market_id="m1",
                     valid_at=observed, observed_at=observed)

    t1, t2, t3 = T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    for price, ts in ((0.50, t1), (0.55, t2), (0.60, t3)):
        log.append(q(price, ts))

    # "What did we know at t2?" must not see the t3 quote.
    state = log.as_of("m1", t2)
    assert state[("kalshi", "yes", "ask")].price == 0.55
    state = log.as_of("m1", t3)
    assert state[("kalshi", "yes", "ask")].price == 0.60

    hist = log.history("m1", "kalshi", "yes", "ask")
    assert [x.price for x in hist] == [0.50, 0.55, 0.60]


def test_market_book_walks_the_ladder():
    book = MarketBook(
        venue="kalshi", venue_market_id="T1", outcome_id="yes",
        asks=[OrderBookLevel(0.50, 100), OrderBookLevel(0.52, 100)],
    )
    assert abs(book.avg_fill_price(150) - (100 * 0.50 + 50 * 0.52) / 150) < 1e-9
    assert book.avg_fill_price(250) is None  # insufficient depth


def test_fair_value_confidence_rewards_sharp_breadth_recency():
    now = T0 - timedelta(hours=1)
    sharp = build_fair_value(
        "m1", "yes",
        [FairSource("pinnacle", 3.0, 0.60),
         FairSource("draftkings", 1.0, 0.58),
         FairSource("fanduel", 1.0, 0.62)],
        starts_at=T0, now=now,
    )
    thin = build_fair_value(
        "m1", "yes", [FairSource("fanduel", 1.0, 0.60)],
        starts_at=T0 + timedelta(days=3), now=now,
    )
    assert abs(sharp.prob - 0.60) < 1e-9
    assert sharp.confidence > thin.confidence
    assert 0.0 <= thin.confidence <= 1.0
