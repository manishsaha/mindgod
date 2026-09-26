"""Sportsbook odds via The Odds API (licensed feed; never scraped).

Translates each book's h2h/spread/total markets into canonical outcomes with
the builders, so equality with the outcomes that listings point at is
structural. Uses the shared event_id_for so feed outcomes match seeded
listings for the same game. Each priced outcome also carries the book's
settlement terms: a whole-number line refunds pushes and a half-point line
does not, so the two are never mixed into one fair value.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from mindgod.application.ports import PricedOutcome, QuotaStatus, SportsbookSource
from mindgod.domain.primitives import Observation
from mindgod.domain.propositions import (
    Comparator,
    Outcome,
    Quantity,
    Threshold,
    margin_exactly,
    moneyline,
    spread,
    total,
)
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, League, Period, TeamId
from mindgod.domain.stats import Stat
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import ListingKey, VenueId

from .listings import event_id_for
from .teams import team_abbr

log = logging.getLogger("mindgod.adapters.odds_api")

BASE = "https://api.the-odds-api.com/v4"
SPORT_LEAGUE = {"americanfootball_nfl": League.NFL, "baseball_mlb": League.MLB}


def _header_int(value: str | None) -> int | None:
    """Parse a quota header; None when the API does not send it."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _valid_at(market: dict[str, Any], book: dict[str, Any], now: datetime) -> datetime:
    """When the book's quote held: the market-level last_update the API reports.

    The bookmaker-level field is deprecated upstream; it is only a fallback.
    """
    for source in (market.get("last_update"), book.get("last_update")):
        if not source:
            continue
        try:
            return datetime.fromisoformat(str(source).replace("Z", "+00:00"))
        except ValueError:
            continue
    return now


def _total_push_outcome(league: League, event: Event, line: Decimal) -> Outcome:
    # The domain exposes margin_exactly for pushes on integer lines but no
    # total-exactly builder. Outcome canonicalizes on construction, so this
    # stays in canonical form without one.
    quantity = Quantity(league, event.id, Stat.SCORE, Period.FULL_GAME)
    return Outcome(quantity, Threshold(Comparator.EQ, line))


def _whole(number: Decimal) -> bool:
    return number == number.to_integral_value()


class OddsApiSource(SportsbookSource):
    def __init__(
        self,
        api_key: str,
        sports: tuple[str, ...] = ("americanfootball_nfl",),
        bookmakers: str = "draftkings,fanduel,pinnacle",
        markets: str = "h2h,spreads,totals",
        regions: str = "us,eu",
    ) -> None:
        self._api_key = api_key
        self._sports = sports
        self._bookmakers = bookmakers
        self._markets = markets
        self._regions = regions
        self._mlb_warned: set[str] = set()
        self._quota: QuotaStatus | None = None

    def quota_status(self) -> QuotaStatus | None:
        return self._quota

    async def priced_outcomes(self) -> list[PricedOutcome]:
        now = datetime.now(UTC)
        out: list[PricedOutcome] = []
        async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
            for sport in self._sports:
                league = SPORT_LEAGUE[sport]
                try:
                    resp = await client.get(
                        f"/sports/{sport}/odds",
                        params={
                            "apiKey": self._api_key,
                            "regions": self._regions,
                            "markets": self._markets,
                            "oddsFormat": "american",
                            "bookmakers": self._bookmakers,
                        },
                    )
                    resp.raise_for_status()
                    events = resp.json()
                except Exception:
                    log.exception("odds api failed for %s", sport)
                    continue
                books = sorted(
                    {
                        str(book.get("key", "unknown"))
                        for data in events
                        for book in data.get("bookmakers", [])
                    }
                )
                log.info(
                    "odds api %s: %d events, bookmakers=%s",
                    sport,
                    len(events),
                    ",".join(books) if books else "none",
                )
                self._quota = QuotaStatus(
                    used=_header_int(resp.headers.get("x-requests-last")),
                    remaining=_header_int(resp.headers.get("x-requests-remaining")),
                    bookmakers=tuple(books),
                )
                out.extend(self._parse_sport(league, events, now))
        return out

    def _parse_sport(
        self, league: League, events: list[dict[str, Any]], now: datetime
    ) -> list[PricedOutcome]:
        parsed: list[tuple[dict[str, Any], str, str, datetime]] = []
        for data in events:
            home = team_abbr(league, str(data.get("home_team", "")))
            away = team_abbr(league, str(data.get("away_team", "")))
            if home is None or away is None:
                log.warning("unknown team in %s", data.get("id"))
                continue
            try:
                scheduled = datetime.fromisoformat(
                    str(data.get("commence_time", "")).replace("Z", "+00:00")
                )
            except ValueError:
                log.warning("bad commence_time in %s", data.get("id"))
                continue
            parsed.append((data, home, away, scheduled))
        numbers = _game_numbers(parsed)
        out: list[PricedOutcome] = []
        for (data, home, away, scheduled), game_number in zip(parsed, numbers, strict=True):
            event = Event(
                id=event_id_for(league, home, away, scheduled.date().isoformat(), game_number),
                league=league,
                home=TeamId(f"{league.value}-{home.lower()}"),
                away=TeamId(f"{league.value}-{away.lower()}"),
                scheduled_start=scheduled,
                game_number=game_number,
            )
            for book in data.get("bookmakers", []):
                venue = VenueId(str(book.get("key", "unknown")))
                for market in book.get("markets", []):
                    out.extend(self._parse_market(league, event, venue, book, market, now))
        return out

    def _parse_market(
        self,
        league: League,
        event: Event,
        venue: VenueId,
        book: dict[str, Any],
        market: dict[str, Any],
        now: datetime,
    ) -> list[PricedOutcome]:
        key = str(market.get("key", ""))
        valid_at = _valid_at(market, book, now)
        out: list[PricedOutcome] = []
        for entry in market.get("outcomes", []):
            built = self._parse_outcome(league, event, key, entry)
            if built is None:
                continue
            outcome, side, terms = built
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
                        observed=Observation(valid_at, now),
                    ),
                    market_group=f"{venue}:{event.id}:{key}",
                    terms=terms,
                )
            )
        return out

    def _parse_outcome(
        self,
        league: League,
        event: Event,
        market_key: str,
        entry: dict[str, Any],
    ) -> tuple[Outcome, str, Terms] | None:
        name = str(entry.get("name", ""))
        refunds: Outcome | None = None
        if market_key == "h2h":
            abbr = team_abbr(league, name)
            team = _team_for_abbr(event, abbr)
            if team is None:
                return None
            outcome = moneyline(event, team)
            # NFL games can tie: the book refunds the stake. MLB games play
            # on, so there is no tie refund to carry.
            refunds = margin_exactly(event, team, Decimal(0)) if league is League.NFL else None
            # ADR-0009: MLB moneylines settle on listed pitchers at some books
            # and as action at others; neither feed reports the rule, so we
            # emit UNKNOWN and let the application decide compatibility.
            if league is League.MLB:
                from mindgod.domain.terms import PitcherRule, VoidPolicy

                return (
                    outcome,
                    str(abbr),
                    Terms(
                        Payoff(outcome, refunds),
                        VoidPolicy(pitcher_rule=PitcherRule.UNKNOWN),
                    ),
                )
            return outcome, str(abbr), Terms(Payoff(outcome, refunds))
        if market_key == "spreads":
            abbr = team_abbr(league, name)
            team = _team_for_abbr(event, abbr)
            if team is None or "point" not in entry:
                return None
            point = Decimal(str(entry["point"]))
            outcome = spread(event, team, point)
            # A whole-number line pushes at exactly the line: the book
            # refunds. A half-point line cannot push. The push margin is
            # -point from the bet side's perspective: KC -3 pushes when KC
            # wins by 3, and BUF +3 pushes on that same game (BUF wins by
            # -3), so both sides must carry identical refund terms or the
            # pair can never be devigged together.
            refunds = margin_exactly(event, team, -point) if _whole(point) else None
            return outcome, str(abbr), Terms(Payoff(outcome, refunds))
        if market_key == "totals":
            if "point" not in entry:
                return None
            point = Decimal(str(entry["point"]))
            comparator = Comparator.GT if name.lower() == "over" else Comparator.LT
            outcome = total(event, comparator, point)
            refunds = _total_push_outcome(event.league, event, point) if _whole(point) else None
            return outcome, name.lower(), Terms(Payoff(outcome, refunds))
        return None


def _game_numbers(
    parsed: list[tuple[dict[str, Any], str, str, datetime]],
) -> list[int]:
    """Number same-day matchups by start time: the doubleheader game_number.

    Both games of a doubleheader share teams and date; ordering by start
    assigns them stable numbers that match the configured listing's.
    """
    by_game: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for idx, (_, home, away, scheduled) in enumerate(parsed):
        by_game[(scheduled.date().isoformat(), home, away)].append(idx)
    numbers = [1] * len(parsed)
    for idxs in by_game.values():
        ordered = sorted(idxs, key=lambda i: parsed[i][3])
        for n, idx in enumerate(ordered, start=1):
            numbers[idx] = n
    return numbers


def _team_for_abbr(event: Event, abbr: str | None) -> TeamId | None:
    if abbr is None:
        return None
    for team in (event.home, event.away):
        if str(team).split("-", 1)[1].upper() == abbr:
            return team
    return None
