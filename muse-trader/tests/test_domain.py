"""Each test protects a betting fact the domain model must get right."""

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from mindgod.domain.combos import Combo
from mindgod.domain.fees import QuadraticFeeModel
from mindgod.domain.primitives import ContractPrice, Observation, Probability
from mindgod.domain.propositions import (
    Comparator,
    margin_exactly,
    moneyline,
    player_stat,
    spread,
    total,
)
from mindgod.domain.quotes import OrderBook, PriceLevel
from mindgod.domain.sports import Event, EventId, League, Period, PlayerId, TeamId
from mindgod.domain.stats import Stat
from mindgod.domain.terms import Payoff, Terms, TermsDifference, VoidPolicy, differences
from mindgod.domain.valuation import FairValue
from mindgod.domain.venues import ListingKey, VenueId

NOW = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
KC, BUF = TeamId("nfl-kc"), TeamId("nfl-buf")
NFL_GAME = Event(EventId("nfl-2026-10-04-buf-at-kc"), League.NFL, KC, BUF, NOW)
NYY, BOS = TeamId("mlb-nyy"), TeamId("mlb-bos")
MLB_NIGHTCAP = Event(EventId("mlb-2026-09-26-bos-at-nyy-g2"), League.MLB, NYY, BOS, NOW, 2)
KALSHI_KEY = ListingKey(VenueId("kalshi"), "KXNFLGAME-TEST", "yes")


class TestProbability:
    def test_american_odds_include_the_vig(self) -> None:
        assert Probability.from_american_odds(-110).value == pytest.approx(0.52381, abs=1e-5)
        assert Probability.from_american_odds(+150).value == pytest.approx(0.4)

    def test_impossible_odds_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            Probability.from_american_odds(50)


class TestCanonicalOutcomes:
    def test_integer_and_half_point_lines_share_the_winning_outcome(self) -> None:
        # Book "KC -3" wins on exactly the results where "KC wins by more than 3.5" wins.
        assert spread(NFL_GAME, KC, D("-3")) == spread(NFL_GAME, KC, D("-3.5"))

    def test_away_side_is_the_complement_of_the_home_side(self) -> None:
        assert spread(NFL_GAME, BUF, D("3.5")) == spread(NFL_GAME, KC, D("-3.5")).complement()

    def test_moneyline_complement_includes_the_tie(self) -> None:
        assert moneyline(NFL_GAME, KC).complement() != moneyline(NFL_GAME, BUF)

    def test_mlb_full_game_moneyline_complement_is_the_away_moneyline(self) -> None:
        # MLB full games cannot end tied, so "margin <= 0" is "margin <= -1":
        # the complement of "NYY wins" is exactly the book's "BOS wins".
        # Without this, MLB moneyline closing lines never pair and go missing.
        assert moneyline(MLB_NIGHTCAP, NYY).complement() == moneyline(MLB_NIGHTCAP, BOS)
        assert moneyline(MLB_NIGHTCAP, BOS).complement() == moneyline(MLB_NIGHTCAP, NYY)

    def test_mlb_first_five_can_tie_so_complement_keeps_zero(self) -> None:
        # First-five innings can tie: no collapsing, unlike the full game.
        assert moneyline(MLB_NIGHTCAP, NYY, Period.FIRST_FIVE_INNINGS).complement() != moneyline(
            MLB_NIGHTCAP, BOS, Period.FIRST_FIVE_INNINGS
        )

    def test_ladder_rungs_imply_lower_rungs(self) -> None:
        over_50_5 = total(NFL_GAME, Comparator.GT, D("50.5"))
        over_44_5 = total(NFL_GAME, Comparator.GT, D("44.5"))
        assert over_50_5.implies(over_44_5)
        assert not over_44_5.implies(over_50_5)

    def test_stats_are_validated_per_league(self) -> None:
        judge = PlayerId("mlb-aaron-judge")
        player_stat(MLB_NIGHTCAP, NYY, judge, Stat.TOTAL_BASES, Comparator.GT, D("1.5"))
        with pytest.raises(ValueError):
            player_stat(MLB_NIGHTCAP, NYY, judge, Stat.PASSING_YARDS, Comparator.GT, D("1.5"))

    def test_teams_must_be_playing(self) -> None:
        with pytest.raises(ValueError):
            moneyline(NFL_GAME, NYY)


class TestTerms:
    def test_nfl_tie_rule_makes_moneylines_different_bets(self) -> None:
        exchange = Terms(Payoff(moneyline(NFL_GAME, KC)))
        book = Terms(Payoff(moneyline(NFL_GAME, KC), margin_exactly(NFL_GAME, KC, D(0))))
        assert differences(exchange, book) == {TermsDifference.REFUND}

    def test_key_number_push_is_the_only_gap_between_minus_3_and_minus_3_5(self) -> None:
        book = Terms(Payoff(spread(NFL_GAME, KC, D("-3")), margin_exactly(NFL_GAME, KC, D(3))))
        exchange = Terms(Payoff(spread(NFL_GAME, KC, D("-3.5"))))
        assert differences(book, exchange) == {TermsDifference.REFUND}

    def test_listed_pitchers_change_the_bet(self) -> None:
        action = Terms(Payoff(moneyline(MLB_NIGHTCAP, NYY)))
        listed = Terms(
            Payoff(moneyline(MLB_NIGHTCAP, NYY)),
            VoidPolicy(listed_pitchers=frozenset({PlayerId("p1"), PlayerId("p2")})),
        )
        assert differences(action, listed) == {TermsDifference.LISTED_PITCHERS}


class TestFees:
    fees = QuadraticFeeModel(taker_rate=D("0.07"), maker_rate=D("0"))

    def test_fee_peaks_at_even_money(self) -> None:
        assert self.fees.taker_fee(ContractPrice(D("0.50")), 100) == D("1.75")
        assert self.fees.taker_fee(ContractPrice(D("0.90")), 100) == D("0.63")

    def test_small_orders_pay_a_rounding_penalty(self) -> None:
        assert self.fees.taker_fee(ContractPrice(D("0.50")), 1) == D("0.02")


class TestDepthAndEdge:
    book = OrderBook(
        KALSHI_KEY,
        asks=(
            PriceLevel(ContractPrice(D("0.45")), 100),
            PriceLevel(ContractPrice(D("0.48")), 200),
        ),
        bids=(),
        observed=Observation(NOW, NOW),
    )

    def test_average_price_worsens_with_size(self) -> None:
        assert self.book.cost_to_buy(100).average_price == D("0.45")  # type: ignore[union-attr]
        assert self.book.cost_to_buy(300).average_price == D("0.47")  # type: ignore[union-attr]
        assert self.book.cost_to_buy(301) is None

    def test_expected_value_is_net_of_fees(self) -> None:
        fill = self.book.cost_to_buy(100)
        assert fill is not None
        fee = QuadraticFeeModel(D("0.07"), D("0")).taker_fee(fill.worst_price, fill.contracts)
        fair = FairValue(moneyline(NFL_GAME, KC), Probability(0.55), 0.01, NOW, "test")
        assert fair.expected_value_per_contract(fill, fee) == D("0.0826")


class TestCombos:
    def test_same_game_combo_is_detected(self) -> None:
        legs = frozenset({moneyline(NFL_GAME, KC), total(NFL_GAME, Comparator.GT, D("47.5"))})
        assert Combo(legs).is_same_game


class TestPitcherRule:
    """ADR-0009: pitcher rule compatibility."""

    def test_equal_rules_compatible(self) -> None:
        from mindgod.domain.terms import PitcherRule, pitcher_rules_compatible

        assert pitcher_rules_compatible(PitcherRule.ACTION, PitcherRule.ACTION)
        assert pitcher_rules_compatible(PitcherRule.LISTED, PitcherRule.LISTED)
        assert pitcher_rules_compatible(PitcherRule.UNKNOWN, PitcherRule.UNKNOWN)

    def test_unknown_vs_action_compatible(self) -> None:
        from mindgod.domain.terms import PitcherRule, pitcher_rules_compatible

        assert pitcher_rules_compatible(PitcherRule.UNKNOWN, PitcherRule.ACTION)
        assert pitcher_rules_compatible(PitcherRule.LISTED, PitcherRule.ACTION)

    def test_action_vs_unknown_incompatible(self) -> None:
        from mindgod.domain.terms import PitcherRule, pitcher_rules_compatible

        # ACTION book vs UNKNOWN venue: the venue might void, the book won't.
        # This is not the safe direction.
        assert not pitcher_rules_compatible(PitcherRule.ACTION, PitcherRule.UNKNOWN)
        assert not pitcher_rules_compatible(PitcherRule.ACTION, PitcherRule.LISTED)
