"""Config loading tests: YAML sections and listing specs parse as written."""

from pathlib import Path

from mindgod.adapters.config import load_settings


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_loads_listing_specs_from_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
execution:
  mode: paper
listings:
  - venue: kalshi
    market_id: KXNFLGAME-26SEP27LACBUF-BUF
    side: yes
    league: nfl
    home: BUF
    away: LAC
    start: "2026-09-27T17:00:00Z"
    outcome_kind: moneyline
    outcome_team: home
""",
    )
    settings = load_settings(path)
    assert settings.execution.mode == "paper"
    assert len(settings.listings) == 1
    spec = settings.listings[0]
    assert spec.market_id == "KXNFLGAME-26SEP27LACBUF-BUF"
    assert spec.home == "BUF"
    assert spec.away == "LAC"


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
listings:
  - venue: kalshi
    market_id: T1
    bogus_key: 123
""",
    )
    settings = load_settings(path)
    assert settings.listings[0].market_id == "T1"


def test_week4_deploy_config_loads_all_listings() -> None:
    path = Path(__file__).resolve().parent.parent / "deploy" / "config.week4.yaml"
    settings = load_settings(path)
    assert settings.execution.mode == "paper"
    assert len(settings.listings) == 15
    ids = [spec.market_id for spec in settings.listings]
    assert len(set(ids)) == 15
    assert "KXNFLGAME-26SEP28PHICHI-CHI" in ids
