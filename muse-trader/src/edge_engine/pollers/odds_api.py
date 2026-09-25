"""Reference sportsbook odds via The Odds API.

GET https://api.the-odds-api.com/v4/sports/{sport}/odds
  ?regions=us,eu&markets=h2h&oddsFormat=american&bookmakers=draftkings,fanduel,pinnacle
Auth: apiKey query param. Credit cost = markets x regions per request.
"""
from __future__ import annotations

import httpx

from ..pricing.devig import american_to_prob, devig
from .base import MarketSnapshot, Poller

BASE = "https://api.the-odds-api.com/v4"


class OddsApiPoller(Poller):
    venue = "odds-api"

    def __init__(self, api_key: str, sports: tuple[str, ...] = ("americanfootball_nfl",),
                 bookmakers: str = "draftkings,fanduel,pinnacle") -> None:
        self.api_key = api_key
        self.sports = sports
        self.bookmakers = bookmakers

    async def poll(self) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
            for sport in self.sports:
                resp = await client.get(
                    f"/sports/{sport}/odds",
                    params={
                        "apiKey": self.api_key,
                        "regions": "us,eu",
                        "markets": "h2h",
                        "oddsFormat": "american",
                        "bookmakers": self.bookmakers,
                    },
                )
                resp.raise_for_status()
                for event in resp.json():
                    home, away = event.get("home_team", ""), event.get("away_team", "")
                    for book in event.get("bookmakers", []):
                        for market in book.get("markets", []):
                            if market.get("key") != "h2h":
                                continue
                            outcomes = market.get("outcomes", [])
                            if len(outcomes) != 2:
                                continue
                            for o in outcomes:
                                snapshots.append(
                                    MarketSnapshot(
                                        event_key=f"{sport}:{event.get('id')}",
                                        venue=book.get("key", "book"),
                                        market_id=event.get("id", ""),
                                        label=f"{away} @ {home}",
                                        outcome="yes",
                                        price=american_to_prob(o["price"]),
                                        raw={"team": o["name"], "american": o["price"]},
                                    )
                                )
        return snapshots


def fair_from_snapshots(
    snapshots: list[MarketSnapshot],
    method: str = "power",
    sharp_books: tuple[str, ...] = ("pinnacle",),
    sharp_weight: float = 3.0,
) -> dict[str, float]:
    """Group book snapshots by event and team, de-vig each book, weight consensus."""
    from ..pricing.devig import consensus

    by_event: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for s in snapshots:
        team = s.raw.get("team", "")
        by_event.setdefault(s.event_key, {}).setdefault(team, []).append(
            (s.venue, s.price)
        )
    fair: dict[str, float] = {}
    for event_key, teams in by_event.items():
        per_book: dict[str, list[float]] = {}
        team_order: list[str] = []
        for team, quotes in teams.items():
            if team not in team_order:
                team_order.append(team)
            for venue, price in quotes:
                per_book.setdefault(venue, []).append(price)
        book_fairs: dict[str, list[float]] = {}
        for venue, probs in per_book.items():
            if len(probs) == len(team_order):
                book_fairs[venue] = devig(probs, method)
        for i, team in enumerate(team_order):
            weighted = [
                (fairs[i], sharp_weight if v in sharp_books else 1.0)
                for v, fairs in book_fairs.items()
            ]
            if weighted:
                fair[f"{event_key}:{team}"] = consensus(weighted)
    return fair
