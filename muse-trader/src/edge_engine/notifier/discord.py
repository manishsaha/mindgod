"""Discord webhook notifier with 429 backoff and cooldown dedup."""
from __future__ import annotations

import asyncio
import time

import httpx

from ..engine.ev import Signal


class DiscordNotifier:
    def __init__(self, webhook_url: str, cooldown_s: int = 900,
                 min_edge_move: float = 0.01) -> None:
        self.webhook_url = webhook_url
        self.cooldown_s = cooldown_s
        self.min_edge_move = min_edge_move
        self._last_sent: dict[str, tuple[float, float]] = {}  # key -> (ts, edge)

    def _key(self, signal: Signal) -> str:
        return f"{signal.venue}:{signal.event_key}:{signal.side}"

    def should_send(self, signal: Signal) -> bool:
        key = self._key(signal)
        last = self._last_sent.get(key)
        if last is None:
            return True
        ts, edge = last
        if time.time() - ts > self.cooldown_s:
            return True
        return abs(signal.edge - edge) >= self.min_edge_move

    def _payload(self, signal: Signal) -> dict:
        color = 0x2ECC71 if signal.edge > 0.05 else 0xF1C40F
        return {
            "username": "Edge Engine",
            "embeds": [
                {
                    "title": f"+EV signal: {signal.side.upper()} {signal.event_key}",
                    "color": color,
                    "fields": [
                        {"name": "Venue", "value": signal.venue, "inline": True},
                        {"name": "Market price",
                         "value": f"{signal.market_price:.1%}", "inline": True},
                        {"name": "Fair value",
                         "value": f"{signal.fair_prob:.1%}", "inline": True},
                        {"name": "Edge", "value": f"{signal.edge:.2%}",
                         "inline": True},
                        {"name": "EV / $1",
                         "value": f"${signal.ev_per_dollar:.3f}", "inline": True},
                        {"name": "Suggested stake",
                         "value": f"${signal.stake:.2f}", "inline": True},
                    ],
                    "footer": {"text": signal.refs or "edge-engine"},
                }
            ],
        }

    async def send(self, signal: Signal) -> bool:
        if not self.should_send(signal):
            return False
        async with httpx.AsyncClient(timeout=15) as client:
            for attempt in range(4):
                resp = await client.post(
                    self.webhook_url, json=self._payload(signal))
                if resp.status_code in (200, 204):
                    self._last_sent[self._key(signal)] = (time.time(),
                                                          signal.edge)
                    return True
                if resp.status_code == 429:
                    retry = resp.json().get("retry_after", 1.0)
                    await asyncio.sleep(float(retry) + 0.5)
                    continue
                resp.raise_for_status()
        return False
