# Core Requirements

Version 0.1, 2026-09-24. Scaffolding phase.

## Goal

Run a persistent service that (1) polls prediction-market odds, (2) prices each
market against sportsbook reference odds, (3) identifies +EV opportunities
against the market maker, (4) notifies a Discord channel, and (5) can execute
orders on the exchange when explicitly enabled.

## Functional requirements

- **FR-1 Multi-venue polling.** Poll Kalshi (official REST API), Polymarket
  (Gamma for discovery, CLOB for orderbook/prices), and reference sportsbook
  odds (FanDuel, DraftKings, Pinnacle via The Odds API or equivalent aggregator)
  on independent intervals. Each poller is isolated: one venue failing must not
  stop the others.
- **FR-2 Normalization.** Convert every venue's quote into a canonical
  `MarketSnapshot`: event id, outcome labels, yes/no prices in [0,1], bid/ask or
  last, timestamp, venue, fees. Handle American/decimal/fractional odds and
  contract prices uniformly.
- **FR-3 Market mapping.** Resolve the same real-world proposition across
  venues (Kalshi ticker, Polymarket slug/token id, sportsbook event id) via a
  mapping table plus fuzzy name/date matching with manual override. Unmapped
  markets are priced but never auto-signaled.
- **FR-4 Fair-value pricing.** Build a fair probability per outcome from
  reference books: strip the vig (de-vig each book), then take a weighted
  consensus (Pinnacle/sharp weight highest). Expose per-book fair probs for
  audit.
- **FR-5 Edge detection.** A signal fires when the NET edge, gross edge minus
  per-contract taker fee minus expected slippage, clears `min_net_edge`. The
  threshold is on net edge, never on gross edge: the taker fee is
  price-dependent (peaks at 50c, shrinks toward the tails), so the minimum
  worthwhile gross edge varies by price. Both directions (buy Yes, buy No)
  are evaluated.
- **FR-6 Sizing.** Stake via fractional Kelly on the edge, capped by
  max_stake_per_bet, max_exposure_per_event, and daily loss limit. Zero or
  negative EV never sizes.
- **FR-7 Discord notifications.** Send rich-embed alerts (market, side, price,
  fair value, edge, EV, suggested stake, links) with dedup: one alert per
  market+side per cooldown window, plus updates when edge moves materially.
  Honor Discord rate limits with 429 backoff.
- **FR-8 Execution adapters.** Three modes: `dry-run` (log only, default),
  `paper` (simulate fills at quoted prices, track P&L), `live` (place real
  orders; requires explicit config flag AND per-run confirmation). Live mode is
  never the default and never implied.
- **FR-9 State persistence.** Record every signal, alert, and fill with
  timestamps. Survive restarts without duplicate alerts or double fills.
  Local SQLite for dev, DynamoDB in AWS.
- **FR-10 Observability.** Structured logs, poll latency and error-rate metrics,
  health endpoint, CloudWatch alarms in AWS. Separate ops alerts (service down)
  from user alerts (edge found).

## Non-functional requirements

- **NFR-1 Latency.** Poll-to-alert under 60s for the default 1-minute cadence;
  sub-10s path reserved for in-play markets later.
- **NFR-2 Cost.** AWS baseline target $10-50/mo (Fargate 0.25 vCPU/0.5 GB,
  DynamoDB on-demand). Avoid NAT Gateway unless the threat model needs it.
- **NFR-3 Secrets.** API keys, private keys, and the Discord webhook URL live in
  AWS Secrets Manager (env vars locally), never in git or logs.
- **NFR-4 Rate-limit citizenship.** Respect each venue's published limits with
  token buckets and exponential backoff. Kalshi Basic tier allows roughly
  20 reads/s and 10 writes/s; The Odds API is credit-metered per request.
- **NFR-5 Testability.** Unit tests for pricing math and EV/Kelly; recorded
  fixtures for poller parsing; paper-trading replay before any live order.

## Compliance and safety

- Kalshi requires KYC and is CFTC-regulated; Polymarket Global blocks new US
  positions (close-only) while Polymarket US is CFTC-regulated with
  state-level availability (verify NY eligibility before trading).
- Never scrape FanDuel/DraftKings frontends; use licensed aggregators or
  enterprise feeds.
- Gambling involves risk of loss. Position limits and loss limits are
  mandatory, not optional. This tooling is for the account holder's own use.
- Live order placement requires explicit user approval per session, consistent
  with the standing rule: no irreversible external action without a clear yes.

## Open decisions

1. Execution venue priority: Kalshi first (US, CFTC) vs Polymarket US.
2. Starting bankroll, Kelly fraction, per-bet and daily caps.
3. Sports/markets in scope for v1 (suggest: NFL/NBA moneylines and totals).
4. The Odds API tier vs pinnodds.com for the sharp reference line.
5. Discord channel and alert format preferences.
