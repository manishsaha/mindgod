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
from mindgod.application.ports import Notifier, OpsPoster
from mindgod.application.pricing import outcome_key

log = logging.getLogger("mindgod.adapters.discord")


class DiscordNotifier(Notifier):
    def __init__(
        self,
        webhook_url: str,
        cooldown_s: int = 900,
        min_edge_move: Decimal = Decimal("0.01"),
        min_x_move: Decimal = Decimal("0.01"),
    ) -> None:
        self._webhook_url = webhook_url
        self._cooldown_s = cooldown_s
        self._min_edge_move = min_edge_move
        self._min_x_move = min_x_move
        self._last_sent: dict[str, tuple[float, Decimal, Decimal | None]] = {}

    def _key(self, opp: Opportunity) -> str:
        # ADR-0008: deduplicate by canonical outcome across venues.
        return outcome_key(opp.outcome)

    def should_send(self, opp: Opportunity) -> bool:
        last = self._last_sent.get(self._key(opp))
        if last is None:
            return True
        ts, edge, last_x = last
        if time.time() - ts > self._cooldown_s:
            return True
        if abs(opp.edge_net - edge) >= self._min_edge_move:
            return True
        # Re-alert only when X moves materially.
        if opp.limit_price is not None and last_x is not None:
            return abs(opp.limit_price - last_x) >= self._min_x_move
        return False

    def _payload(self, opp: Opportunity) -> dict[str, Any]:
        key = opp.listing
        fair = opp.fair_value
        color = 0x2ECC71 if opp.edge_net > Decimal("0.05") else 0xF1C40F
        # ADR-0008 alert format: price limit, not price.
        if opp.limit_price is not None:
            x_cents = int(opp.limit_price * 100)
            title = (
                f"Buy up to {opp.fill.contracts} of {key.market_id} "
                f"({key.side}) at \u2264 {x_cents}\u00a2"
            )
            fields: list[dict[str, Any]] = [
                {
                    "name": "Fair value",
                    "value": (
                        f"{float(fair.probability.value):.1%} "
                        f"(\u00b1{float(fair.standard_error):.3f})"
                    ),
                    "inline": True,
                },
                {
                    "name": "Net edge at ask",
                    "value": f"{float(opp.edge_net):.2%}",
                    "inline": True,
                },
                {
                    "name": f"Depth at \u2264 {x_cents}\u00a2",
                    "value": str(opp.depth_at_x),
                    "inline": True,
                },
                {
                    "name": "Ask at alert",
                    "value": f"{float(opp.fill.average_price):.3f}",
                    "inline": True,
                },
                {
                    "name": "Fee",
                    "value": f"${float(opp.fee):.2f}",
                    "inline": True,
                },
                {
                    "name": "Stake",
                    "value": f"${float(opp.stake):.2f}",
                    "inline": True,
                },
            ]
        else:
            title = f"+EV: {key.market_id} ({key.side})"
            fields = [
                {"name": "Venue", "value": str(key.venue_id), "inline": True},
                {
                    "name": "Outcome",
                    "value": repr(opp.outcome)[:1024],
                    "inline": False,
                },
                {
                    "name": "Fill (avg)",
                    "value": f"{float(opp.fill.average_price):.3f}",
                    "inline": True,
                },
                {
                    "name": "Fair value",
                    "value": (
                        f"{float(fair.probability.value):.1%} (se {float(fair.standard_error):.3f})"
                    ),
                    "inline": True,
                },
                {
                    "name": "Net edge",
                    "value": f"{float(opp.edge_net):.2%}",
                    "inline": True,
                },
                {
                    "name": "Contracts",
                    "value": str(opp.fill.contracts),
                    "inline": True,
                },
                {
                    "name": "Stake",
                    "value": f"${float(opp.stake):.2f}",
                    "inline": True,
                },
                {
                    "name": "Fee",
                    "value": f"${float(opp.fee):.2f}",
                    "inline": True,
                },
            ]
        return {
            "username": "MindGod",
            "embeds": [
                {
                    "title": title,
                    "color": color,
                    "fields": fields,
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
                    resp = await client.post(self._webhook_url, json=self._payload(opp))
                except Exception:
                    log.exception("discord post failed")
                    return False
                if resp.status_code in (200, 204):
                    self._last_sent[self._key(opp)] = (
                        time.time(),
                        opp.edge_net,
                        opp.limit_price,
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


class DiscordOpsPoster(OpsPoster):
    """Plain-text posts to the ops channel: credit burn, poll failures.

    Never raises: ops telemetry must not take down the loop that reports it.
    """

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def post(self, text: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self._webhook_url,
                    json={"username": "MindGod-ops", "content": text},
                )
                resp.raise_for_status()
        except Exception:
            log.exception("ops webhook post failed")
