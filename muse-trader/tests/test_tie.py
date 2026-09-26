"""ADR-0011: the NFL moneyline tie adjustment.

Required tests from the ADR:
- -150/+130: after conversion, home > away, and home + away + t = 1.
- Kalshi "home wins" Yes and No fair values sum to 1, with the tie on the
  No side.
- The conversion never touches spreads, totals, MLB, or non-FULL_GAME
  periods.
- The closing-line pairing forms for an NFL moneyline under the rule and
  produces the converted value.

The first test also reproduces the B1 bug: without the rule, the listing's
terms key has no fair value at all.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mindgod.adapters.store import Store
from mindgod.application.ports import ListingKey, PricedOutcome
from mindgod.application.pricing import (
    WeightedConsensusModel,
    devig,
    outcome_key,
    terms_key,
)
from mindgod.application.service import ServiceContext, capture_closing_lines
from mindgod.application.tie import (
    NFL_TIE_PROB,
    find_tie_adjusted_fair,
    tie_aware_counterpart,
    tie_close_basis,
    yes_basis,
)
from mindgod.domain.primitives import Observation, Probability
from mindgod.domain.propositions import Comparator, margin_exactly, moneyline, spread, total
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, EventId, League, Period, TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import Listing, VenueId

NOW = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
KALSHI = VenueId("kalshi")
DK = VenueId("draftkings")
FD = VenueId("fanduel")
KC = TeamId("nfl-kc")
BUF = TeamId("nfl-buf")
GAME = Event(EventId("nfl-kc-buf-20261005"), League.NFL, KC, BUF, NOW)


def _book_priced(book: str, team: TeamId, odds: int) -> PricedOutcome:
    """A book moneyline quote with tie-refund terms, as the odds adapter builds them."""
    key = ListingKey(VenueId(book), "game", f"h2h:{team}")
    outcome = moneyline(GAME, team)
    terms = Terms(Payoff(outcome, margin_exactly(GAME, team, Decimal(0))))
    return PricedOutcome(
        outcome=outcome,
        listing_key=key,
        quote=SportsbookQuote(key, odds, Observation(NOW, NOW)),
        market_group=f"{book}:game:h2h",
        terms=terms,
    )


def _kalshi_listing(outcome, side: str) -> Listing:
    """A Kalshi-style listing: settles No on a tie, no refund."""
    return Listing(
        key=ListingKey(KALSHI, "KXNFLGAME-26OCT05KCBUF", side),
        terms=Terms(Payoff(outcome)),
    )


def _listing_terms_key(outcome) -> str:
    """The terms key tick() computes for a Kalshi-style listing."""
    return terms_key(Terms(Payoff(outcome, None)))


def test_no_fair_value_without_the_rule():
    """Reproduces B1: the book outcome matches, the terms key does not."""
    priced = [_book_priced("draftkings", KC, -150), _book_priced("draftkings", BUF, 130)]
    model = WeightedConsensusModel(method="power")
    fair_by_terms = model.values_by_terms(priced, NOW)
    yes_home = moneyline(GAME, KC)
    partition = fair_by_terms.get(_listing_terms_key(yes_home))
    assert partition is None or partition[1].get(yes_home) is None


def test_partition_carries_terms_without_key_parsing():
    """values_by_terms returns each partition's Terms alongside its values.

    The tie rule reads the book's refund terms from the object, not by
    parsing the terms key string back apart. Each side's partition carries
    its own refund outcome.
    """
    priced = [_book_priced("draftkings", KC, -150), _book_priced("draftkings", BUF, 130)]
    model = WeightedConsensusModel(method="power")
    fair_by_terms = model.values_by_terms(priced, NOW)
    yes_home = moneyline(GAME, KC)
    yes_away = moneyline(GAME, BUF)
    by_refund = {
        terms.payoff.refunds_if: by_outcome for terms, by_outcome in fair_by_terms.values()
    }
    assert by_refund[margin_exactly(GAME, KC, Decimal(0))][yes_home].probability.value > 0
    assert by_refund[margin_exactly(GAME, BUF, Decimal(0))][yes_away].probability.value > 0


def test_tie_conversion_sums_with_tie():
    """-150/+130: home > away after conversion, and home + away + t = 1."""
    priced = [_book_priced("draftkings", KC, -150), _book_priced("draftkings", BUF, 130)]
    model = WeightedConsensusModel(method="power")
    fair_by_terms = model.values_by_terms(priced, NOW)
    yes_home = moneyline(GAME, KC)
    yes_away = moneyline(GAME, BUF)
    listing_terms = Terms(Payoff(yes_home))

    home_fair = find_tie_adjusted_fair(
        fair_by_terms, yes_outcome=yes_home, listing_terms=listing_terms
    )
    away_fair = find_tie_adjusted_fair(
        fair_by_terms, yes_outcome=yes_away, listing_terms=listing_terms
    )
    assert home_fair is not None and away_fair is not None
    home_p = float(home_fair.probability.value)
    away_p = float(away_fair.probability.value)
    assert home_p > away_p  # the favorite stays the favorite
    assert home_p + away_p + NFL_TIE_PROB == pytest.approx(1.0)

    # The conversion is exactly the devigged book price times (1 - t).
    devigged = devig(
        [
            Probability.from_american_odds(-150).value,
            Probability.from_american_odds(130).value,
        ],
        "power",
    )
    assert home_p == pytest.approx(devigged[0] * (1.0 - NFL_TIE_PROB))
    assert away_p == pytest.approx(devigged[1] * (1.0 - NFL_TIE_PROB))
    # Lineage marks the adjustment; SE carries the tie-rate uncertainty.
    assert home_fair.method.endswith("+tie-adj")
    assert home_fair.standard_error >= 0.003


def test_yes_no_sum_to_one_with_tie_on_no_side():
    """Kalshi Yes and No fair values sum to 1; the tie mass sits on No."""
    priced = [_book_priced("draftkings", KC, -150), _book_priced("draftkings", BUF, 130)]
    model = WeightedConsensusModel(method="power")
    fair_by_terms = model.values_by_terms(priced, NOW)
    yes_home = moneyline(GAME, KC)
    yes_away = moneyline(GAME, BUF)
    listing_terms = Terms(Payoff(yes_home))

    home_fair = find_tie_adjusted_fair(
        fair_by_terms, yes_outcome=yes_home, listing_terms=listing_terms
    )
    away_fair = find_tie_adjusted_fair(
        fair_by_terms, yes_outcome=yes_away, listing_terms=listing_terms
    )
    assert home_fair is not None and away_fair is not None
    home_p = float(home_fair.probability.value)
    away_p = float(away_fair.probability.value)
    # tick() derives the No-side fair as 1 - P(yes).
    no_p = 1.0 - home_p
    assert home_p + no_p == pytest.approx(1.0)
    assert no_p == pytest.approx(away_p + NFL_TIE_PROB)


def test_conversion_ignores_non_moneylines():
    """Spreads, totals, MLB, and non-full-game never convert."""
    priced = [_book_priced("draftkings", KC, -150), _book_priced("draftkings", BUF, 130)]
    model = WeightedConsensusModel(method="power")
    fair_by_terms = model.values_by_terms(priced, NOW)

    # NFL spread yes basis: margin >= 4.
    spread_outcome = spread(GAME, KC, Decimal("-3.5"))
    assert (
        find_tie_adjusted_fair(
            fair_by_terms,
            yes_outcome=spread_outcome,
            listing_terms=Terms(Payoff(spread_outcome)),
        )
        is None
    )
    # Total.
    total_outcome = total(GAME, Comparator.GT, Decimal("47.5"))
    assert (
        find_tie_adjusted_fair(
            fair_by_terms,
            yes_outcome=total_outcome,
            listing_terms=Terms(Payoff(total_outcome)),
        )
        is None
    )
    # MLB moneyline: ties are impossible, so there is nothing to adjust.
    mlb_game = Event(
        EventId("mlb-nyy-bos-20261005"), League.MLB, TeamId("mlb-nyy"), TeamId("mlb-bos"), NOW
    )
    mlb_outcome = moneyline(mlb_game, TeamId("mlb-nyy"))
    assert (
        find_tie_adjusted_fair(
            fair_by_terms,
            yes_outcome=mlb_outcome,
            listing_terms=Terms(Payoff(mlb_outcome)),
        )
        is None
    )
    # First half: ties are possible but the rule is scoped to full games.
    half_outcome = moneyline(GAME, KC, Period.FIRST_HALF)
    assert (
        find_tie_adjusted_fair(
            fair_by_terms,
            yes_outcome=half_outcome,
            listing_terms=Terms(Payoff(half_outcome)),
        )
        is None
    )
    # A listing that already refunds ties needs no conversion.
    yes_home = moneyline(GAME, KC)
    refunding = Terms(Payoff(yes_home, margin_exactly(GAME, KC, Decimal(0))))
    assert (
        find_tie_adjusted_fair(fair_by_terms, yes_outcome=yes_home, listing_terms=refunding) is None
    )


def test_yes_basis_and_counterpart_shapes():
    yes_home = moneyline(GAME, KC)  # margin >= 1
    yes_away = moneyline(GAME, BUF)  # margin <= -1
    assert yes_basis(yes_home) == yes_home
    assert yes_basis(yes_away) == yes_away
    no_home = yes_home.complement()  # margin <= 0
    assert no_home is not None
    assert yes_basis(no_home) == yes_home
    # The counterpart is the book's other side, not the exact complement.
    counter = tie_aware_counterpart(yes_home)
    assert counter == yes_away
    assert tie_aware_counterpart(yes_away) == yes_home
    # Spreads do not pair under the rule.
    assert tie_aware_counterpart(spread(GAME, KC, Decimal("-3.5"))) is None
    # Listings: the rule governs Kalshi-style no-tie-refund moneylines.
    assert tie_close_basis(_kalshi_listing(yes_home, "yes")) == yes_home
    assert tie_close_basis(_kalshi_listing(no_home, "no")) == yes_home
    refunding = Listing(
        key=ListingKey(KALSHI, "X", "yes"),
        terms=Terms(Payoff(yes_home, margin_exactly(GAME, KC, Decimal(0)))),
    )
    assert tie_close_basis(refunding) is None


class _FakeExchange:
    def __init__(self, listings):
        self.venue_id = KALSHI
        self._listings = listings

    async def discover(self):
        return []

    async def order_book(self, market_id):
        return None


class _FakeResolver:
    def __init__(self, listings, event):
        self._listings = listings
        self._event = event
        self.review_queue = []

    def listings_for(self, venue_id):
        return [lst for lst in self._listings if lst.key.venue_id == venue_id]

    def event_start(self, key):
        return self._event.scheduled_start

    def resolve(self, key):
        return None

    def register(self, listing): ...

    def report_unmapped(self, market): ...


def _record_moneyline_quotes(store, event):
    """-150/+130 moneyline quotes from two books, confirmed before kickoff."""
    yes_home = moneyline(event, KC)
    yes_away = moneyline(event, BUF)
    valid_at = event.scheduled_start - timedelta(hours=2)
    recorded_at = event.scheduled_start - timedelta(minutes=15)
    priced = []
    for venue in (DK, FD):
        for outcome, odds in ((yes_home, -150), (yes_away, 130)):
            key = ListingKey(venue_id=venue, market_id="test", side="yes")
            team = KC if outcome == yes_home else BUF
            terms = Terms(Payoff(outcome, margin_exactly(event, team, Decimal(0))))
            priced.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=key,
                    quote=SportsbookQuote(key, odds, Observation(valid_at, recorded_at)),
                    market_group=f"{venue}:test:h2h",
                    terms=terms,
                )
            )
    store.record_priced(priced, recorded_at)


def test_closing_line_pairs_under_tie_rule():
    """The close forms from the tie-aware pair and carries the conversion."""
    store = Store(":memory:")
    event = Event(
        EventId("nfl-kc-buf-20261006"),
        League.NFL,
        KC,
        BUF,
        datetime(2026, 10, 6, 17, 0, tzinfo=UTC),
    )
    _record_moneyline_quotes(store, event)
    yes_home = moneyline(event, KC)
    no_home = yes_home.complement()
    assert no_home is not None
    listings = [
        _kalshi_listing(yes_home, "yes"),
        _kalshi_listing(no_home, "no"),
    ]
    model = WeightedConsensusModel(method="power", max_quote_age_s=3600)
    ctx = ServiceContext(
        sportsbook=None,
        exchanges=[_FakeExchange(listings)],
        execution={},
        resolver=_FakeResolver(listings, event),
        model=model,
        detector=None,  # type: ignore[arg-type]
        risk=None,  # type: ignore[arg-type]
        store=store,
    )
    at = event.scheduled_start + timedelta(minutes=30)
    assert capture_closing_lines(ctx, at) == 2

    devigged_home = devig(
        [
            Probability.from_american_odds(-150).value,
            Probability.from_american_odds(130).value,
        ],
        "power",
    )[0]
    expected_yes = devigged_home * (1.0 - NFL_TIE_PROB)
    yes_close = store.get_closing_line(outcome_key(yes_home))
    assert yes_close is not None
    assert yes_close["sharp_close_prob"] == pytest.approx(expected_yes)
    # The No side carries the tie mass back.
    no_close = store.get_closing_line(outcome_key(no_home))
    assert no_close is not None
    assert no_close["sharp_close_prob"] == pytest.approx(1.0 - expected_yes)
