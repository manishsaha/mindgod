"""Configuration: YAML file layered over environment variables.

Fee rates, edge thresholds, horizons, and risk limits are configuration,
not code. They change over time and differ across markets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass(frozen=True, slots=True)
class FeeModelConfig:
    taker_rate: str = "0.07"
    maker_rate: str = "0.0"


@dataclass(frozen=True, slots=True)
class EngineConfig:
    min_net_edge: str = "0.02"
    kelly_fraction: str = "0.25"
    max_stake_per_bet: str = "100.0"
    max_exposure_per_event: str = "500.0"
    slippage: str = "0.005"
    uncertainty_aversion: str = "10.0"
    threshold_widening: str = "2.0"
    horizon_days: int = 7
    # Move check: suppress an opportunity when the exchange mid moved more
    # than this (probability) since the last sportsbook refresh.
    max_kalshi_move: float = 0.03
    # Spread gate: skip the move check when the book's spread exceeds this.
    move_check_max_spread: float = 0.06
    # ADR-0008: paper fills model human reaction time.
    reaction_delay_s: int = 60
    # Discovery only feeds the review queue; restrict it to the series we
    # actually price instead of pulling every kind of market.
    kalshi_discovery_series: list[str] = field(default_factory=lambda: ["KXNFLGAME", "KXMLBGAME"])


@dataclass(frozen=True, slots=True)
class PricingConfig:
    devig_method: str = "power"
    book_weights: dict[str, float] = field(default_factory=lambda: {"pinnacle": 3.0})
    # Floor on the fair-value standard error: a single book measures zero
    # disagreement, which would drop the uncertainty penalty exactly when
    # uncertainty is highest.
    min_standard_error: float = 0.02
    # Freshness: quotes older than this never become fair values, and quotes
    # approaching the gate widen the error bar by stale_se_per_minute.
    max_quote_age_s: int = 900
    stale_se_per_minute: float = 0.001
    # ADR-0009: extra SE (in quadrature) when the book's pitcher rule is
    # UNKNOWN or LISTED vs the venue's ACTION.
    pitcher_rule_se: float = 0.01


@dataclass(frozen=True, slots=True)
class PollingConfig:
    exchange_interval_s: int = 60
    sportsbook_interval_s: int = 300
    # Discovery only feeds the human review queue; it never trades.
    discovery_interval_s: int = 3600


@dataclass(frozen=True, slots=True)
class AlertsConfig:
    cooldown_s: int = 900
    min_edge_move: str = "0.01"


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    mode: str = "dry-run"  # dry-run | paper | live


@dataclass(frozen=True, slots=True)
class ListingSpec:
    """One registered market: a venue ticker mapped onto a canonical outcome.

    outcome_team is "home" or "away" for moneyline/spread. For side "no" the
    outcome is complemented at build time: buying the listing's "yes" is
    buying the other side of the proposition.
    """

    venue: str
    market_id: str
    side: str = "yes"
    league: str = "nfl"
    home: str = "KC"
    away: str = "BUF"
    start: str = ""
    game_number: int = 1
    outcome_kind: str = "moneyline"  # moneyline | spread | total
    outcome_team: str = "home"
    handicap: str = "0"
    total_line: str = "0"
    total_side: str = "over"  # over | under
    refunds_on_tie: bool = False
    token_id: str = ""  # Polymarket CLOB token id, when known


@dataclass(frozen=True, slots=True)
class Settings:
    polling: PollingConfig = field(default_factory=PollingConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    fees: dict[str, FeeModelConfig] = field(default_factory=dict)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    listings: tuple[ListingSpec, ...] = ()


def _build[T](cls: type[T], data: Any) -> T:
    if not isinstance(data, dict):
        return cls()
    # Introspect the class, never instantiate it to probe: some specs
    # (ListingSpec) have required fields, so cls() raises TypeError and
    # no config with listings could ever load.
    known = {f.name for f in fields(cast(Any, cls))}
    return cls(**{k: v for k, v in data.items() if k in known})


class _Loader(yaml.SafeLoader):
    """YAML loader with 1.2 core-schema booleans.

    yaml.SafeLoader implements YAML 1.1, where unquoted yes/no/on/off become
    booleans. That corrupts config strings: `side: yes` becomes True and the
    Saints' team code `home: NO` becomes False, crashing listing setup. YAML
    1.2 only treats true/false as booleans, so every other scalar stays a
    string. Real bool fields (e.g. refunds_on_tie) keep working as long as
    they are written true/false.
    """


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in mappings if tag != "tag:yaml.org,2002:bool"]
    for first, mappings in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        for candidate in ("config.yaml", "config.example.yaml"):
            if Path(candidate).exists():
                path = candidate
                break
    if path is None:
        return Settings()
    with open(path, encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=_Loader) or {}
    return Settings(
        polling=_build(PollingConfig, data.get("polling")),
        pricing=_build(PricingConfig, data.get("pricing")),
        engine=_build(EngineConfig, data.get("engine")),
        fees={
            venue: _build(FeeModelConfig, cfg) for venue, cfg in (data.get("fees") or {}).items()
        },
        alerts=_build(AlertsConfig, data.get("alerts")),
        execution=_build(ExecutionConfig, data.get("execution")),
        listings=tuple(_build(ListingSpec, spec) for spec in (data.get("listings") or [])),
    )
