# Architecture

## Data flow

```
pollers (Kalshi, Polymarket Gamma/CLOB, The Odds API)
        |
        v
normalizer -> MarketSnapshot(event_key, outcomes, prices, ts, venue)
        |
        v
market mapper -> links snapshots of the same proposition (mapping table +
                 fuzzy match, manual override; unmapped = priced only)
        |
        v
pricing engine -> de-vig each reference book -> weighted consensus fair prob
        |
        v
EV engine -> edge = |fair - market| - fees - slippage
           -> fractional Kelly sizing, caps, daily loss limit
           -> Signal(market, side, price, fair, edge, ev, stake)
        |
        +--> notifier -> Discord webhook (dedup + cooldown)
        |
        +--> execution adapter -> dry-run | paper | live (explicit only)
        |
        v
state store -> signals, alerts, fills (SQLite local / DynamoDB on AWS)
```

## Components

- **Pollers** (`pollers/`): one class per venue implementing `Poller.poll()`.
  Independent asyncio tasks with per-venue intervals and jitter. Failures are
  caught per poller and counted as metrics, never fatal.
- **Pricing** (`pricing/`): odds-format conversion, de-vigging (additive,
  multiplicative, power), book consensus with sharp weighting.
- **Engine** (`engine/`): EV per contract, edge threshold, fractional Kelly,
  signal construction and dedup keys.
- **Notifier** (`notifier/discord.py`): webhook POST with embeds, 429 backoff,
  per-market cooldown.
- **Execution** (`execution/broker.py`): `Broker` interface. `DryRunBroker`
  logs. `PaperBroker` simulates fills at quote and tracks P&L.
  `KalshiBroker` / `PolymarketBroker` are stubs until auth is wired; live
  requires `execution.mode: live` plus a runtime confirmation flag.
- **Storage** (`storage/state.py`): SQLite locally; the schema mirrors what
  DynamoDB will hold (signals, alerts, fills keyed by market+side+ts).
- **Scheduler** (`scheduler.py`): the main loop. One tick = poll all, map,
  price, evaluate, notify, execute. Configurable cadence per venue.

## Domain model (`domain/`)

The foundation everything downstream stands on. Venue data is never trusted
directly: it is normalized into domain objects, linked to a canonical market
through the mapper, and only then priced or traded.

- **CanonicalEvent / CanonicalMarket / Outcome** (`events.py`, `markets.py`):
  one real-world event (a game), one proposition about it (Chiefs moneyline),
  and the list of outcomes with optional lines. Market types include
  moneyline, spread, total, prop, and combo (with `combo_legs` pointing at
  the canonical leg markets, the home of the correlation detectors later).
- **SettlementRules** (`settlement.py`): first-class, not an afterthought.
  Overtime, DNP voids, stat-correction windows, postponement rules, and the
  rule source are data, with a SHA fingerprint. Two markets are only
  comparable when fingerprints match or an explicit equivalence is
  registered.
- **MarketMapper** (`mapping.py`): entity resolution across venues. Explicit
  registration is the source of truth; `suggest()` ranks candidates for
  human confirmation when bootstrapping. `resolve()` never guesses: unknown
  ids return None, and a venue quote whose settlement fingerprint changed
  under us is quarantined (priced, never signaled) and recorded in
  `mismatches`. This is the trap from the research made impossible by
  construction: a "different bet wearing a similar name" cannot reach the
  EV engine.
- **Quote / QuoteLog** (`quotes.py`, `log.py`): bitemporal, append-only.
  Every observed price keeps `valid_at` (when it was valid at the venue) and
  `observed_at` (when we saw it); the gap is the latency the stale-quote
  detectors will trade against. `as_of(ts)` answers "what did we know at
  time T", which makes backtests honest and enables closing line value:
  our fill price vs the sharp consensus at close. The scheduler already
  appends every polled snapshot; canonical linking arrives with the mapper
  integration.
- **MarketBook** (`quotes.py`): order-book levels with `avg_fill_price()`,
  which walks the ladder. Depth-aware sizing plugs in here: a 4% edge on
  the first $200 that is 0% by $2,000 must size against the ladder, not
  top of book.
- **FairValue** (`fairvalue.py`): consensus probability plus a confidence
  score blending sharp-book weight share, book breadth, cross-book
  agreement, and time to event. Confidence gates thresholds and sizing
  downstream instead of living only in a log line.

## AWS deployment (target)

- Docker image -> Amazon ECR.
- Long-running **ECS Fargate service** (0.25 vCPU / 0.5 GB), internal asyncio
  loop. Desired count 1; health check + CloudWatch alarm restarts it.
- Secrets (Kalshi key id, Polymarket creds, Odds API key, Discord webhook) in
  **Secrets Manager**, injected via the task definition. Least-privilege task
  roles.
- **DynamoDB** on-demand tables: `signals`, `alerts`, `fills`.
- **EventBridge Scheduler** for batch jobs (daily P&L summary, mapping refresh).
- **CloudWatch Logs** via awslogs; alarms on task stopped / error rate / no
  signals for N hours (stale-data detector).
- Cost: Fargate small task is a few dollars a month; the usual cost driver is
  NAT Gateway (~$30+/mo), so prefer public subnets with `assignPublicIp` for
  v1 or VPC endpoints for Secrets Manager/DynamoDB.
- See `infra/aws/README.md` for the concrete steps.

## Local dev

`docker compose` is optional; plain `python -m edge_engine.scheduler` with
SQLite works. Paper mode replays recorded fixtures so the EV engine can be
tested without venue access.
