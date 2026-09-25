# Edge Engine

A persistent service that polls prediction-market odds (Kalshi, Polymarket),
prices them against sportsbook reference odds (FanDuel, DraftKings, Pinnacle via
aggregators), identifies +EV opportunities with a fair-value model, notifies via
Discord, and optionally executes through exchange APIs.

## Layout

- `src/edge_engine/pollers/` - venue adapters (Kalshi, Polymarket Gamma/CLOB,
  The Odds API)
- `src/edge_engine/pricing/` - odds normalization, de-vigging, fair-value consensus
- `src/edge_engine/engine/` - EV computation, edge detection, Kelly sizing, signals
- `src/edge_engine/notifier/` - Discord webhook alerts
- `src/edge_engine/execution/` - order execution adapters (dry-run default)
- `src/edge_engine/storage/` - signal/alert state
- `src/edge_engine/scheduler.py` - main polling loop entrypoint
- `docs/` - requirements, architecture, API reference
- `infra/aws/` - AWS hosting notes (ECS Fargate)

## Quickstart (local, dry-run)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # fill in API keys
cp config.example.yaml config.yaml
python -m edge_engine.scheduler --config config.yaml --dry-run
pytest
```

The service always starts in dry-run mode unless `--live` is passed with an
explicitly enabled execution adapter. Nothing places real orders by default.

## Status

Scaffolding phase. Pollers, pricing, and EV engine have working cores with
tests. Venue auth, market mapping, and AWS deployment are the next milestones.
See `docs/requirements.md`.
