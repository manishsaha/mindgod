"""Composition root: wire adapters into the application service.

This is the only place that knows about concrete adapters. Everything else
depends on ports and the domain.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from decimal import Decimal

from mindgod.adapters.config import PollingConfig, load_settings
from mindgod.adapters.discord import DiscordNotifier
from mindgod.adapters.execution import make_execution
from mindgod.adapters.kalshi import KalshiExchange
from mindgod.adapters.listings import RegistryResolver, build_event, build_listing
from mindgod.adapters.odds_api import OddsApiSource
from mindgod.adapters.polymarket import PolymarketExchange
from mindgod.adapters.store import Store
from mindgod.application.opportunities import DetectorConfig, ValueDetector
from mindgod.application.ports import ExecutionVenue
from mindgod.application.pricing import WeightedConsensusModel
from mindgod.application.risk import ExposureLimits
from mindgod.application.service import ServiceContext, run_forever
from mindgod.domain.fees import QuadraticFeeModel
from mindgod.domain.venues import VenueId

log = logging.getLogger("mindgod")


def build_context(
    config_path: str | None, bankroll: Decimal, live_flag: bool
) -> tuple[ServiceContext, PollingConfig]:
    settings = load_settings(config_path)

    resolver = RegistryResolver()
    token_ids: dict[str, str] = {}
    for spec in settings.listings:
        listing = build_listing(spec)
        resolver.register(listing)
        resolver.note_event(listing.key, build_event(spec))
        if spec.token_id:
            token_ids[spec.market_id] = spec.token_id

    fees = {
        VenueId("kalshi"): QuadraticFeeModel(Decimal("0.07"), Decimal("0")),
        # Polymarket's taker fee varies by market; 3% is an approximation to
        # verify per market before production. Conservative: keep ceil rounding.
        VenueId("polymarket"): QuadraticFeeModel(Decimal("0.03"), Decimal("0")),
    }
    for venue, cfg in settings.fees.items():
        fees[VenueId(venue)] = QuadraticFeeModel(Decimal(cfg.taker_rate), Decimal(cfg.maker_rate))

    kalshi = KalshiExchange()
    polymarket = PolymarketExchange(token_ids)

    odds_key = os.environ.get("ODDS_API_KEY", "")
    sportsbook = OddsApiSource(api_key=odds_key) if odds_key else None
    if sportsbook is None:
        log.warning("ODDS_API_KEY not set: running without sportsbook prices")

    execution: dict[VenueId, ExecutionVenue] = {
        VenueId("kalshi"): make_execution(settings.execution.mode, live_flag=live_flag),
        VenueId("polymarket"): make_execution(settings.execution.mode, live_flag=live_flag),
    }

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    notifier = (
        DiscordNotifier(
            webhook,
            cooldown_s=settings.alerts.cooldown_s,
            min_edge_move=Decimal(settings.alerts.min_edge_move),
        )
        if webhook
        else None
    )

    engine = settings.engine
    detector = ValueDetector(
        fees,
        DetectorConfig(
            bankroll=bankroll,
            min_net_edge=Decimal(engine.min_net_edge),
            kelly_fraction=Decimal(engine.kelly_fraction),
            max_stake_per_bet=Decimal(engine.max_stake_per_bet),
            slippage=Decimal(engine.slippage),
            uncertainty_aversion=Decimal(engine.uncertainty_aversion),
            threshold_widening=Decimal(engine.threshold_widening),
        ),
        refs="mindgod",
    )

    ctx = ServiceContext(
        sportsbook=sportsbook,
        exchanges=[kalshi, polymarket],
        execution=execution,
        resolver=resolver,
        model=WeightedConsensusModel(
            method=settings.pricing.devig_method,
            book_weights=settings.pricing.book_weights,
            min_standard_error=settings.pricing.min_standard_error,
        ),
        detector=detector,
        risk=ExposureLimits(max_exposure_per_event=Decimal(engine.max_exposure_per_event)),
        store=Store(os.environ.get("MINDGOD_DB_PATH", "mindgod.db")),
        notifier=notifier,
        horizon_days=engine.horizon_days,
    )
    return ctx, settings.polling


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="MindGod edge service")
    parser.add_argument("--config", default=None)
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument(
        "--live",
        action="store_true",
        help="required together with execution.mode=live for real orders",
    )
    args = parser.parse_args()
    ctx, polling = build_context(args.config, Decimal(str(args.bankroll)), args.live)
    asyncio.run(
        run_forever(
            ctx,
            exchange_interval_s=polling.exchange_interval_s,
            sportsbook_interval_s=polling.sportsbook_interval_s,
            discovery_interval_s=polling.discovery_interval_s,
        )
    )


if __name__ == "__main__":
    main()
