"""Sportsbook odds via The Odds API (licensed feed; never scraped).

Translates each book's h2h/spread/total markets into canonical outcomes with
the builders, so equality with the outcomes that listings point at is
structural. Uses the shared event_id_for so feed outcomes match seeded
listings for the same game.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from mindgod.application.ports import PricedOutcome, SportsbookSource
from mindgod.domain.primitives import Observation
from mindgod.domain.propositions import (
    Comparator,
    Outcome,
    moneyline,
    spread,
    total,
)
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, League, TeamId
from mindgod.domain.venues import ListingKey, VenueId

from .listings import event_id_for
from .teams import team_abbr

log = logging.getLogger("mindgod.adapters.odds_api")

BASE = "https://api.the-odds-api.com/v4"
SPORT_LEAGUE = {"americanfootball_nfl": League.NFL, "baseball_mlb": League.MLB}


class OddsApiSource(SportsbookSource):
    def __init__(
        self,
        api_key: str,
        sports: tuple[str, ...] = ("americanfootball_nfl",),
        bookmakers: str = "draftkings,fanduel,pinnacle",
        markets: str = "h2h,spreads,totals",
    ) -> None:
        self._api_key = api_key
        self._sports = sports
        self._bookmakers = bookmakers
        self._markets = markets

    async def priced_outcomes(self) -> list[PricedOutcome]:
        now = datetime.now(UTC)
        out: list[PricedOutcome] = []
        async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
            for sport in self._sports:
                league = SPORT_LEAGUE[sport]
                resp = await client.get(
                    f"/sports/{sport}/odds",
                    params={
                        "apiKey": self._api_key,
                        "regions": "us,eu",
                        "markets": self._markets,
                        "oddsFormat": "american",
                        "bookmakers": self._bookmakers,
                    },
                )
                resp.raise_for_status()
                for event_data in resp.json():
                    out.extend(self._parse_event(league, event_data, now))
        return out

    def _parse_event(
        self, league: League, data: dict[str, Any], now: datetime
    ) -> list[PricedOutcome]:
        home = team_abbr(league, str(data.get("home_team", "")))
        away = team_abbr(league, str(data.get("away_team", "")))
        if home is None or away is None:
            log.warning("unknown team in %s", data.get("id"))
            return []
        try:
            scheduled = datetime.fromisoformat(
                str(data.get("commence_time", "")).replace("Z", "+00:00")
            )
        except ValueError:
            log.warning("bad commence_time in %s", data.get("id"))
            return []
        event = Event(
            id=event_id_for(league, home, away, scheduled),
            league=league,
            home=TeamId(f"{league.value}-{home.lower()}"),
            away=TeamId(f"{league.value}-{away.lower()}"),
            scheduled_start=scheduled,
        )
        out: list[PricedOutcome] = []
        for book in data.get("bookmakers", []):
            venue = VenueId(str(book.get("key", "unknown")))
            for market in book.get("markets", []):
                out.extend(self._parse_market(league, event, venue, market, now))
        return out

    def _parse_market(
        self,
        league: League,
        event: Event,
        venue: VenueId,
        market: dict[str, Any],
        now: datetime,
    ) -> list[PricedOutcome]:
        key = str(market.get("key", ""))
        out: list[PricedOutcome] = []
        for entry in market.get("outcomes", []):
            built = self._parse_outcome(league, event, key, entry)
            if built is None:
                continue
            outcome, side = built
            listing_key = ListingKey(
                venue_id=venue,
                market_id=str(event.id),
                side=f"{key}:{side}",
            )
            try:
                price = int(entry["price"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=listing_key,
                    quote=SportsbookQuote(
                        listing=listing_key,
                        american_odds=price,
                        observed=Observation(now, now),
                    ),
                    market_group=f"{venue}:{event.id}:{key}",
                )
            )
        return out

    def _parse_outcome(
        self,
        league: League,
        event: Event,
        market_key: str,
        entry: dict[str, Any],
    ) -> tuple[Outcome, str] | None:
        name = str(entry.get("name", ""))
        if market_key == "h2h":
            team = _team_for_abbr(event, team_abbr(league, name))
            if team is None:
                return None
            return moneyline(event, team), str(team_abbr(league, name))
        if market_key == "spreads":
            team = _team_for_abbr(event, team_abbr(league, name))
            if team is None or "point" not in entry:
                return None
            return (
                spread(event, team, Decimal(str(entry["point"]))),
                str(team_abbr(league, name)),
            )
        if market_key == "totals":
            if "point" not in entry:
                return None
            comparator = (
                Comparator.GT if name.lower() == "over" else Comparator.LT
            )
            return (
                total(event, comparator, Decimal(str(entry["point"]))),
                name.lower(),
            )
        return None


def _team_for_abbr(event: Event, abbr: str | None) -> TeamId | None:
    if abbr is None:
        return None
    for team in (event.home, event.away):
        if str(team).split("-", 1)[1].upper() == abbr:
            return team
    return None
