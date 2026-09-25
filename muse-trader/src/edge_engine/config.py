"""Configuration: YAML file layered over environment variables."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class PollingConfig(BaseModel):
    kalshi_interval_s: int = 60
    polymarket_interval_s: int = 60
    odds_api_interval_s: int = 300


class PricingConfig(BaseModel):
    devig_method: str = "power"
    sharp_books: list[str] = Field(default_factory=lambda: ["pinnacle"])
    sharp_weight: float = 3.0
    book_weight: float = 1.0


class EngineConfig(BaseModel):
    # Threshold applied to NET edge (gross edge minus per-contract taker fee
    # minus slippage). Never apply a flat threshold to gross edge: the taker
    # fee is price-dependent, so the minimum worthwhile gross edge varies.
    min_net_edge: float = 0.02
    kelly_fraction: float = 0.25
    max_stake_per_bet: float = 100.0
    max_exposure_per_event: float = 500.0
    daily_loss_limit: float = 1000.0
    slippage: float = 0.005


class FeeConfig(BaseModel):
    taker_rate: float = 0.07
    round_up_cents: bool = True


class AlertsConfig(BaseModel):
    cooldown_s: int = 900
    min_edge_move: float = 0.01


class ExecutionConfig(BaseModel):
    mode: str = "dry-run"
    live_requires_flag: bool = True


class Settings(BaseModel):
    polling: PollingConfig = PollingConfig()
    pricing: PricingConfig = PricingConfig()
    engine: EngineConfig = EngineConfig()
    # Venue taker-fee schedules. Fees change over time and differ across
    # markets, so they live here, not in code.
    fees: dict[str, FeeConfig] = Field(default_factory=lambda: {
        "kalshi": FeeConfig(taker_rate=0.07, round_up_cents=True),
        "polymarket": FeeConfig(taker_rate=0.03, round_up_cents=False),
    })
    alerts: AlertsConfig = AlertsConfig()
    execution: ExecutionConfig = ExecutionConfig()


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        for candidate in ("config.yaml", "config.example.yaml"):
            if Path(candidate).exists():
                path = candidate
                break
    if path is None:
        return Settings()
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Settings(**data)
