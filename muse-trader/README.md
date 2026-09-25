# MindGod

Sharp +EV sports trading on prediction markets (Kalshi first), priced against
sharp sportsbook and exchange consensus. Leagues: MLB and NFL. Horizon:
positions that settle within a week, with intraday entries and exits.

| Where | What |
|---|---|
| `docs/principles.md` | The betting principles every decision is checked against |
| `docs/domain-model.md` | Canonical domain model and the MLB/NFL edge cases it handles |
| `docs/api-reference.md` | Venue and feed API research (verify before shipping) |
| `docs/architecture.md` | How the pieces fit together |
| `docs/adr/` | Architecture decision records |
| `CLAUDE.md` | Guidance for Claude Code working in this repo |

## Layout

```
src/mindgod/domain       pure model: no I/O, stdlib only
src/mindgod/application  use cases and ports (protocols)
src/mindgod/adapters     Kalshi, Polymarket, odds feeds, storage, Discord
```

Dependencies point inward only: adapters -> application -> domain.

## How a tick works

1. The Odds API adapter translates book prices into canonical outcomes.
2. The pricing model devigs each book's markets and weights books into a
   fair value with a standard error and lineage.
3. Exchange adapters pull order books for registered listings only.
4. The detector gates on net edge (fee- and slippage-aware), sizes with
   fractional Kelly shrunk by uncertainty, and caps by book depth.
5. Opportunities are logged, sent to Discord, and executed per the
   configured mode (dry-run default; live fails closed).

Unregistered markets go to a review queue. They are never traded.

## Develop

```
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest && mypy src && ruff check .
```

## Run

```
cp config.example.yaml config.yaml   # then register listings
export ODDS_API_KEY=... DISCORD_WEBHOOK_URL=...
PYTHONPATH=src python -m mindgod --bankroll 10000
```

Modes: `dry-run` (default, log only), `paper` (simulated fills),
`live` (fails closed until order paths are wired and authorized).
