"""Discord webhook notifier with 429 backoff and cooldown dedup.

Discord is never in the critical path for fleeting edges (ADR-0003); it
reports what the engine decided.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from mindgod.application.opportunities import Opportunity
from mindgod.application.ports import Notifier

log = logging.getLogger("mindgod.adapters.discord")


class DiscordNotifier(Notifier):
    def __init__(
        self,
        webhook_url: str,
        cooldown_s: int = 900,
        min_edge_move: Decimal = Decimal("0.01"),
    ) -> None:
        self._webhook_url = webhook_url
        self._cooldown_s = cooldown_s
        self._min_edge_move = min_edge_move
        self._last_sent: dict[str, tuple[float, Decimal]] = {}

    def _key(self, opp: Opportunity) -> str:
        key = opp.listing
        return f"{key.venue_id}:{key.market_id}:{key.side}"

    def should_send(self, opp: Opportunity) -> bool:
        last = self._last_sent.get(self._key(opp))
        if last is None:
            return True
        ts, edge = last
        if time.time() - ts > self._cooldown_s:
            return True
        return abs(opp.edge_net - edge) >= self._min_edge_move

    def _payload(self, opp: Opportunity) -> dict[str, Any]:
        key = opp.listing
        fair = opp.fair_value
        color = 0x2ECC71 if opp.edge_net > Decimal("0.05") else 0xF1C40F
        return {
            "username": "MindGod",
            "embeds": [
                {
                    "title": f"+EV: {key.market_id} ({key.side})",
                    "color": color,
                    "fields": [
                        {"name": "Venue", "value": str(key.venue_id), "inline": True},
                        {
                            "name": "Outcome",
                            "value": repr(opp.outcome)[:1024],
                            "inline": False,
                        },
                        {
                            "name": "Fill (avg)",
                            "value": f"{opp.fill.average_price:.3f}",
                            "inline": True,
                        },
                        {
                            "name": "Fair value",
                            "value": f"{fair.probability.value:.1%} "
                            f"(se {fair.standard_error:.3f})",
                            "inline": True,
                        },
                        {
                            "name": "Net edge",
                            "value": f"{opp.edge_net:.2%}",
                            "inline": True,
                        },
                        {
                            "name": "Contracts",
                            "value": str(opp.fill.contracts),
                            "inline": True,
                        },
                        {
                            "name": "Stake",
                            "value": f"${opp.stake:.2f}",
                            "inline": True,
                        },
                        {
                            "name": "Fee",
                            "value": f"${opp.fee:.2f}",
                            "inline": True,
                        },
                    ],
                    "footer": {"text": fair.method},
                }
            ],
        }

    async def send(self, opp: Opportunity) -> bool:
        if not self.should_send(opp):
            return False
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(4):
                try:
                    resp = await client.post(
                        self._webhook_url, json=self._payload(opp)
                    )
                except Exception:
                    log.exception("discord post failed")
                    return False
                if resp.status_code in (200, 204):
                    self._last_sent[self._key(opp)] = (
                        time.time(),
                        opp.edge_net,
                    )
                    return True
                if resp.status_code == 429:
                    try:
                        retry = float(resp.json().get("retry_after", 1.0))
                    except Exception:
                        retry = 1.0
                    await asyncio.sleep(retry + 0.5)
                    continue
                log.warning("discord rejected: %s", resp.status_code)
                return False
        return False
