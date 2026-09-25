"""Main polling loop. One tick: poll -> map -> price -> evaluate -> notify -> execute."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

from .config import load_settings
from .domain.log import QuoteLog
from .domain.quotes import snapshot_to_quote
from .engine.ev import evaluate
from .execution.broker import make_broker
from .notifier.discord import DiscordNotifier
from .pollers.kalshi import KalshiPoller
from .pollers.odds_api import OddsApiPoller, fair_from_snapshots
from .pollers.polymarket import PolymarketGammaPoller
from .pricing.fees import for_venue
from .storage.state import StateStore

log = logging.getLogger("edge_engine")

# Manual mapping table: canonical event key -> venue market ids.
# Unmapped markets are priced but never signaled.
MAPPING: dict[str, dict[str, str]] = {
    # "nfl:chiefs-bills-2026-10-05": {"kalshi": "NFL-GAME-...", "polymarket": "0x..."},
}


async def tick(settings, notifier, broker, store, quote_log, bankroll: float) -> None:
    eng = settings.engine
    kalshi = await KalshiPoller().poll()
    poly = await PolymarketGammaPoller().poll()
    log.info("polled %d kalshi, %d polymarket markets", len(kalshi), len(poly))

    # Append-only quote log: every observed price lands here, mapped or not.
    # Canonical market linking comes from the mapper in a later phase.
    now = datetime.now(timezone.utc)
    for snap in kalshi + poly:
        quote_log.append(snapshot_to_quote(snap, None, now))

    odds_key = os.environ.get("ODDS_API_KEY", "")
    fair: dict[str, float] = {}
    if odds_key:
        books = await OddsApiPoller(odds_key).poll()
        for snap in books:
            quote_log.append(snapshot_to_quote(snap, None, now))
        fair = fair_from_snapshots(
            books,
            method=settings.pricing.devig_method,
            sharp_books=tuple(settings.pricing.sharp_books),
            sharp_weight=settings.pricing.sharp_weight,
        )
        log.info("fair values for %d book outcomes", len(fair))

    for snap in kalshi + poly:
        mapping = MAPPING.get(snap.event_key)
        if not mapping or snap.venue not in mapping:
            continue  # unmapped: priced only, never signaled
        fair_prob = fair.get(f"{snap.event_key}:{snap.label}")
        if fair_prob is None:
            continue
        fee_cfg = settings.fees.get(snap.venue)
        fee_schedule = for_venue(
            snap.venue,
            {"taker_rate": fee_cfg.taker_rate,
             "round_up_cents": fee_cfg.round_up_cents} if fee_cfg else None,
        )
        for side in ("yes", "no"):
            signal = evaluate(
                event_key=snap.event_key,
                venue=snap.venue,
                side=side,
                market_price=snap.price,
                fair_prob=fair_prob,
                bankroll=bankroll,
                min_net_edge=eng.min_net_edge,
                kelly_fraction_mult=eng.kelly_fraction,
                max_stake=eng.max_stake_per_bet,
                fee_schedule=fee_schedule,
                slippage=eng.slippage,
                refs="odds-api consensus",
            )
            if signal is None:
                continue
            alerted = await notifier.send(signal) if notifier else False
            store.record_signal(signal, alerted)
            fill = await broker.place(signal)
            if fill is not None:
                store.record_fill(signal.event_key, signal.venue, signal.side,
                                  fill.contracts, fill.fill_price, fill.live)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="allow live execution (also needs config mode=live)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings(args.config)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    notifier = DiscordNotifier(
        webhook,
        cooldown_s=settings.alerts.cooldown_s,
        min_edge_move=settings.alerts.min_edge_move,
    ) if webhook else None
    broker = make_broker(settings.execution.mode, live_flag=args.live)
    store = StateStore()
    quote_log = QuoteLog()

    log.info("starting edge-engine mode=%s bankroll=%.2f",
             settings.execution.mode, args.bankroll)
    while True:
        try:
            await tick(settings, notifier, broker, store, quote_log, args.bankroll)
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            log.exception("tick failed")
        if args.once:
            break
        await asyncio.sleep(min(settings.polling.kalshi_interval_s,
                                settings.polling.polymarket_interval_s))


if __name__ == "__main__":
    asyncio.run(main())
