"""The measurable things a bet can be written on, per league."""

from __future__ import annotations

from enum import StrEnum

from mindgod.domain.sports import League


class Stat(StrEnum):
    """What a Quantity measures.

    SCORE is points (NFL) or runs (MLB). Every numeric stat is integer-valued;
    the canonical outcome form in propositions.py relies on that.
    """

    # Game and team level
    SCORE = "score"
    MARGIN = "margin"  # home score minus away score; moneylines and spreads are thresholds on it

    # NFL player
    PASSING_YARDS = "passing_yards"
    PASSING_TOUCHDOWNS = "passing_touchdowns"
    RUSHING_YARDS = "rushing_yards"
    RECEIVING_YARDS = "receiving_yards"
    RECEPTIONS = "receptions"
    TOUCHDOWNS = "touchdowns"

    # MLB player
    HITS = "hits"
    HOME_RUNS = "home_runs"
    TOTAL_BASES = "total_bases"
    RUNS_BATTED_IN = "runs_batted_in"
    HITS_RUNS_RBIS = "hits_runs_rbis"
    PITCHER_STRIKEOUTS = "pitcher_strikeouts"
    PITCHER_OUTS_RECORDED = "pitcher_outs_recorded"

    # Categorical: the result is a participant, not a number
    FIRST_TOUCHDOWN_SCORER = "first_touchdown_scorer"

    @property
    def is_categorical(self) -> bool:
        return self in _CATEGORICAL


_CATEGORICAL = frozenset({Stat.FIRST_TOUCHDOWN_SCORER})
_GAME_LEVEL = frozenset({Stat.SCORE, Stat.MARGIN})

STATS_BY_LEAGUE: dict[League, frozenset[Stat]] = {
    League.NFL: _GAME_LEVEL
    | {
        Stat.PASSING_YARDS,
        Stat.PASSING_TOUCHDOWNS,
        Stat.RUSHING_YARDS,
        Stat.RECEIVING_YARDS,
        Stat.RECEPTIONS,
        Stat.TOUCHDOWNS,
        Stat.FIRST_TOUCHDOWN_SCORER,
    },
    League.MLB: _GAME_LEVEL
    | {
        Stat.HITS,
        Stat.HOME_RUNS,
        Stat.TOTAL_BASES,
        Stat.RUNS_BATTED_IN,
        Stat.HITS_RUNS_RBIS,
        Stat.PITCHER_STRIKEOUTS,
        Stat.PITCHER_OUTS_RECORDED,
    },
}
