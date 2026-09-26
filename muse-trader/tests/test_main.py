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
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ODDS_API_MARKETS", raising=False)


def test_odds_markets_default(_env):
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._markets == "h2h,spreads,totals"


def test_odds_markets_env_override(_env, monkeypatch):
    monkeypatch.setenv("ODDS_API_MARKETS", "h2h")
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is not None
    assert ctx.sportsbook._markets == "h2h"


def test_no_odds_key_means_no_sportsbook(_env, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY")
    ctx, _ = build_context(str(WEEK4), Decimal("10000"), False)
    assert ctx.sportsbook is None


def test_week4_config_uses_credit_safe_poll_interval():
    # 300s polling at 1 credit/poll burns 876 credits over the ~73h weekend
    # window; 600s keeps it at ~438, inside the 500/month quota.
    cfg = yaml.safe_load(WEEK4.read_text())
    assert cfg["polling"]["sportsbook_interval_s"] == 600
