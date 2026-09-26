"""Tests for the __main__ composition root's environment knobs."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from mindgod.__main__ import build_context

WEEK4 = Path(__file__).resolve().parent.parent / "deploy" / "config.week4.yaml"


@pytest.fixture()
def _env(tmp_path, monkeypatch):
    # build_context writes nothing, but point the DB at tmp anyway and keep
    # real secrets out: the sportsbook only needs a non-empty key here.
    monkeypatch.setenv("MINDGOD_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/calls")
    monkeypatch.setenv("DISCORD_WEBHOOK_OPS", "https://discord.test/ops")
    monkeypatch.delenv("ODDS_API_MARKETS", raising=False)
    monkeypatch.delenv("ODDS_API_REGIONS", raising=False)
    monkeypatch.delenv("ODDS_API_BOOKMAKERS", raising=False)
    monkeypatch.delenv("MINDGOD_SOFT_STARTUP", raising=False)


def test_odds_markets_default(_env):
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._markets == "h2h,spreads,totals"


def test_odds_markets_env_override(_env, monkeypatch):
    monkeypatch.setenv("ODDS_API_MARKETS", "h2h")
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._markets == "h2h"


def test_odds_regions_default(_env):
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._regions == "us,eu"


def test_odds_regions_env_override(_env, monkeypatch):
    monkeypatch.setenv("ODDS_API_REGIONS", "us")
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._regions == "us"


def test_odds_bookmakers_default(_env):
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._bookmakers == "draftkings,fanduel,pinnacle"


def test_odds_bookmakers_env_override(_env, monkeypatch):
    monkeypatch.setenv("ODDS_API_BOOKMAKERS", "pinnacle,circa")
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._bookmakers == "pinnacle,circa"


def test_missing_odds_key_fails_loudly(_env, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY")
    with pytest.raises(SystemExit, match="ODDS_API_KEY"):
        build_context(str(WEEK4), Decimal("10000"), False)


def test_missing_calls_webhook_fails_loudly(_env, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    with pytest.raises(SystemExit, match="DISCORD_WEBHOOK_URL"):
        build_context(str(WEEK4), Decimal("10000"), False)


def test_missing_ops_webhook_fails_loudly(_env, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_OPS")
    with pytest.raises(SystemExit, match="DISCORD_WEBHOOK_OPS"):
        build_context(str(WEEK4), Decimal("10000"), False)


def test_missing_db_path_fails_loudly(_env, monkeypatch):
    monkeypatch.delenv("MINDGOD_DB_PATH")
    with pytest.raises(SystemExit, match="MINDGOD_DB_PATH"):
        build_context(str(WEEK4), Decimal("10000"), False)


def test_soft_startup_flag_allows_missing(_env, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    monkeypatch.delenv("DISCORD_WEBHOOK_OPS")
    monkeypatch.setenv("MINDGOD_SOFT_STARTUP", "1")
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is None
    assert ctx.notifier is None
    assert ctx.ops_poster is None


def test_ops_poster_wired(_env):
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.notifier is not None
    assert ctx.ops_poster is not None


def test_week4_config_uses_credit_safe_poll_interval():
    # 300s polling at 1 credit/poll burns 876 credits over the ~73h weekend
    # window; 600s keeps it at ~438, inside the 500/month quota.
    cfg = yaml.safe_load(WEEK4.read_text())
    assert cfg["polling"]["sportsbook_interval_s"] == 600
