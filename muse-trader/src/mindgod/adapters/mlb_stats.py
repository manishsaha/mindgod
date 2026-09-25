"""MLB Stats API adapter for probable pitchers (ADR-0009).

Fetches announced starters for MLB games. When probables change, or fewer
than two are announced, MLB listings for that game are suppressed until the
next sportsbook refresh after the change.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from mindgod.application.ports import ProbablePitchers, ProbablePitcherSource

log = logging.getLogger("mindgod.adapters.mlb_stats")

# MLB Stats API is public, no key required.
_BASE_URL = "https://statsapi.mlb.com/api/v1"


class MlbStatsProbableSource(ProbablePitcherSource):
    """Fetches probable pitchers from the public MLB Stats API."""

    def __init__(self, timeout_s: float = 10.0) -> None:
        self._timeout_s = timeout_s

    async def probables(self, event_ids: list[str]) -> list[ProbablePitchers]:
        """Fetch probables for the given event IDs.

        Event IDs are expected in the format from event_id_for, e.g.
        "mlb-2026-10-05-nyy-at-bos-g1". We extract the date and teams.
        """
        # Group by date to minimize API calls
        by_date: dict[str, list[str]] = {}
        for eid in event_ids:
            # Parse date from event_id: mlb-YYYY-MM-DD-...
            parts = eid.split("-")
            if len(parts) < 4 or parts[0] != "mlb":
                continue
            date = f"{parts[1]}-{parts[2]}-{parts[3]}"
            by_date.setdefault(date, []).append(eid)

        result: list[ProbablePitchers] = []
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for date, eids in by_date.items():
                try:
                    result.extend(await self._fetch_for_date(client, date, eids))
                except Exception:
                    log.exception("failed to fetch probables for %s", date)
        return result

    async def _fetch_for_date(
        self, client: httpx.AsyncClient, date: str, event_ids: list[str]
    ) -> list[ProbablePitchers]:
        """Fetch the schedule for a date and extract probables."""
        url = f"{_BASE_URL}/schedule"
        params: dict[str, str | int] = {
            "sportId": 1,  # MLB
            "date": date,
            "hydrate": "probablePitcher",
        }
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        # Build a map from team abbreviations to probables
        probables_by_game: dict[str, ProbablePitchers] = {}
        dates = data.get("dates", [])
        if not isinstance(dates, list):
            return []
        for date_entry in dates:
            if not isinstance(date_entry, dict):
                continue
            games = date_entry.get("games", [])
            if not isinstance(games, list):
                continue
            for game in games:
                if not isinstance(game, dict):
                    continue
                teams = game.get("teams", {})
                if not isinstance(teams, dict):
                    continue
                home = teams.get("home", {})
                away = teams.get("away", {})
                if not isinstance(home, dict) or not isinstance(away, dict):
                    continue

                home_pitcher = self._extract_pitcher(home.get("probablePitcher"))
                away_pitcher = self._extract_pitcher(away.get("probablePitcher"))

                # Match to our event IDs by team abbreviations
                for eid in event_ids:
                    if self._matches_game(eid, game):
                        probables_by_game[eid] = ProbablePitchers(
                            event_id=eid,
                            home_pitcher=home_pitcher,
                            away_pitcher=away_pitcher,
                            announced_at=datetime.now(UTC),
                        )
        return list(probables_by_game.values())

    def _extract_pitcher(self, pitcher_data: object) -> str | None:
        if not isinstance(pitcher_data, dict):
            return None
        name = pitcher_data.get("fullName", "")
        return str(name) if name else None

    def _matches_game(self, event_id: str, game: dict[str, object]) -> bool:
        """Check if an event_id matches an MLB Stats API game.

        Event ID format: mlb-YYYY-MM-DD-away-at-home-gN
        We match on date and team abbreviations.
        """
        # Extract teams from event_id
        # e.g., "mlb-2026-10-05-nyy-at-bos-g1" -> away=nyy, home=bos
        parts = event_id.split("-")
        if len(parts) < 7:
            return False
        try:
            at_idx = parts.index("at")
            away_abbr = parts[at_idx - 1].upper()
            home_abbr = parts[at_idx + 1].upper()
        except (ValueError, IndexError):
            return False

        teams = game.get("teams", {})
        if not isinstance(teams, dict):
            return False
        home = teams.get("home", {})
        away = teams.get("away", {})
        if not isinstance(home, dict) or not isinstance(away, dict):
            return False
        home_team = home.get("team", {})
        away_team = away.get("team", {})
        if not isinstance(home_team, dict) or not isinstance(away_team, dict):
            return False
        home_abbr_api = str(home_team.get("abbreviation", "")).upper()
        away_abbr_api = str(away_team.get("abbreviation", "")).upper()

        return home_abbr_api == home_abbr and away_abbr_api == away_abbr
