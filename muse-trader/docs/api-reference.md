# API Reference (condensed from 2026-09-25 research)

Verify hostnames and limits against current official docs before shipping;
several figures below come from secondary sources.

## Kalshi (docs.kalshi.com)

- Base (verify prod host): `https://external-api.kalshi.com/trade-api/v2`
- Auth: asymmetric key pairs, Ed25519 recommended (RSA-2048 also supported).
  Per-request headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms),
  `KALSHI-ACCESS-SIGNATURE` = base64(sign(timestamp_ms + METHOD + path)).
- Market data (public): `GET /markets`, `/markets/{ticker}`,
  `/markets/{ticker}/orderbook`, `/events`, `/exchange/status`.
- Trading (auth): `POST /portfolio/orders`, `DELETE /portfolio/orders/{id}`,
  `GET /portfolio/{balance,positions,fills,orders,settlements}`.
- Rate limits: token buckets, most calls cost 10 tokens. Basic tier 200/100
  tokens per second read/write (about 20 read, 10 write req/s); tiers up to
  Prestige 12000/9600, earned by volume.
- Fees: taker = round_up(0.07 x contracts x price x (1 - price)); makers mostly
  free; no settlement or membership fees. API itself is free.
- Python: `kalshi-python-sync` on PyPI.
- CFTC-regulated DCM; KYC and 18+ required.
- **NY legal risk (verified Sep 2026):** in July 2026 a federal judge (SDNY)
  denied Kalshi's preliminary injunction, ruling NY gambling law is not
  preempted by the CEA for sports event contracts; on Jul 31, 2026 the NY AG
  sued Kalshi for $36B seeking to halt in-state operations. Kalshi continues
  to operate in NY while appealing, but sports contracts (the contested line)
  face live jurisdictional risk. Confirm current status before trading from NY.

## Polymarket

- **Gamma API** (market discovery, free, no auth): `https://gamma-api.polymarket.com`
  - `GET /events`, `/markets`, `/markets/slug/{slug}`, `/public-search?q=`,
    `/tags`, `/series`. Sort params are camelCase (`volume24hr`, `liquidity`).
  - Roughly 4,000 req/10s; markets expose `clobTokenIds` for orderbook calls.
- **CLOB API** (orderbook + trading): `https://clob.polymarket.com`
  - Public: `GET /book?token_id=`, `/price`, `/prices-history`, `/spread`,
    `/midpoint`, `/last-trade-price`.
  - Trading: `POST /order`, `DELETE /order/{id}`, `GET /orders`, `/trades`.
  - Auth two-tier: L1 EIP-712 wallet signature derives API creds; L2 HMAC with
    `POLY_*` headers. Settlement on Polygon in USDC.e.
  - Python: official `py-clob-client`.
  - Fees: taker-only, roughly 3% (sports) to 7% (crypto) of a price-tied
    formula; makers free.
- **US restrictions**: Polymarket Global blocks new positions from US IPs
  (close-only); market data reads are fine worldwide. Polymarket US
  (`polymarket.us`) is CFTC-regulated with KYC, live in 40+ states, blocked in
  AZ, IL, MA, MD, MI, MT, NV, OH. Verify NY eligibility before trading.

## Reference sportsbook odds

- FanDuel and DraftKings offer **no public official odds API**. Do not scrape
  their frontends; use aggregators or enterprise feeds.
- **The Odds API** (`https://api.the-odds-api.com/v4`, key in `apiKey` param):
  - `GET /sports/{sport_key}/odds?regions=us&markets=h2h&oddsFormat=american&bookmakers=draftkings,fanduel,betmgm`
  - Pricing: free 500 credits/mo; $30/20K, $59/100K, $119/5M, $249/15M.
    Live odds cost = markets x regions per request. 100+ sports, 100+ books
    including DraftKings, FanDuel, BetMGM, Caesars, Pinnacle (EU).
  - No official Python SDK; plain REST.
- **Pinnacle direct API closed ~July 2025.** Get Pinnacle lines via The Odds
  API (`regions=eu&bookmakers=pinnacle`) or independent feeds like
  pinnodds.com ($99-229/mo tiers, verify before buying).

## Discord webhooks

- `POST https://discord.com/api/webhooks/{id}/{token}` (token in URL is the
  auth; guard it like a secret). 204 on success.
- Payload: `content` (<=2000 chars) and/or up to 10 embeds (title <=256,
  description <=4096, <=25 fields, <=6000 chars per embed).
- Rate limits are not hard-published; operational guidance is about 30
  requests/min per webhook. Parse `X-RateLimit-*` headers and honor 429
  `retry_after`.

## AWS

- Containerize -> ECR -> **ECS Fargate** long-running service with internal
  poll loop (0.25 vCPU / 0.5 GB is single-digit $/mo).
- Secrets Manager (or SSM SecureString) for keys; DynamoDB on-demand for
  state; EventBridge Scheduler for batch jobs; CloudWatch Logs + alarms.
- Baseline roughly $10-50/mo; NAT Gateway (~$30+/mo) is the usual surprise,
  so avoid it for v1.
