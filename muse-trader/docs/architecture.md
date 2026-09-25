# Architecture

## Layers

```
adapters/      implements ports: Kalshi, Polymarket, The Odds API,
               listing registry, SQLite store, Discord, execution
application/   use cases + ports: pricing, detection, risk, service loop
domain/        pure model (stdlib only): outcomes, terms, quotes,
               fees, fair value, combos
```

Dependencies point inward: adapters -> application -> domain. Venue formats
never leak past an adapter: every venue market is translated into canonical
`Outcome`s (via the builders in `domain/propositions.py`) and `Terms` before
the application ever sees it.

## Tick flow

```
OddsApiSource.priced_outcomes()      sportsbook prices -> PricedOutcome,
        |                            each carrying its settlement Terms;
        |                            refreshed on its own cadence, failures
        |                            keep the previous prices
WeightedConsensusModel.values_by_terms()  devig per book-market *before*
        |                            terms partitioning -> dict[terms_key,
        |                            dict[Outcome, FairValue]]; stale quotes
        |                            dropped, lone sides dropped, age
        |                            widens the error bar; partitions never
        |                            mix
KalshiExchange / PolymarketExchange  order books for registered listings
        |                            (Kalshi: batch /markets/orderbooks,
        |                            bids-only ladders, ask = 1 - other bid)
RegistryResolver                     ticker -> Listing (explicit table);
                                     unknown -> review queue, never traded;
                                     event identity is date + game_number
ValueDetector.detect()               net-edge gate + fractional Kelly +
                                     uncertainty shrink + depth cap, but
                                     only against a fair value whose source
                                     terms equal the listing's terms
        |
ExposureLimits -> execution (dry-run | paper | live-fails-closed)
        |
        +-> Discord notify (async, after execution, off the critical path)
```

## Key design points

- **Canonical outcomes.** "KC -3", "KC wins by more than 3.5", and
  "BUF +3.5" all reduce to thresholds on home-minus-away margin with integer
  normal form, so equality means "same outcome". Settlement differences
  (push refunds, listed pitchers, voids) live in `Terms`, separate from the
  outcome. Two listings are the same bet only when both match.
- **Terms travel with every price.** `PricedOutcome` carries the book's
  settlement terms: a whole-number spread or total line refunds pushes, a
  half-point line cannot push, an NFL moneyline refunds ties. Fair values
  are built inside terms partitions and a listing only uses a fair value
  whose source terms are exactly its own. A 50% devig on KC -3 at -110 is
  conditional on no push; using it against a push-refunding Kalshi listing
  invents edge that does not exist. Devigging runs per market group before
  partitioning (never the reverse), so a terms bug can never silently
  switch the vig removal off; a lone side is dropped rather than passed
  through with the vig intact. See ADR-0005.
- **Stale quotes never become fair values.** Quotes older than
  `pricing.max_quote_age_s` are dropped, aging quotes widen the standard
  error, and an opportunity is suppressed when the exchange mid moved more
  than `engine.max_kalshi_move` since the last sportsbook refresh. When
  news breaks the exchange reprices in seconds while the consensus lags by
  minutes; without these the system would flag the correct new price as
  mispriced. See ADR-0007.
- **Kalshi books are bids only.** `yes_dollars` / `no_dollars` hold resting
  bids, ascending, best last. The yes ask is `1 - best no bid`. Prices and
  counts are fixed-point strings; fractional counts floor to whole
  contracts. Treating a yes bid as a yes ask once printed a fake 5c edge;
  the parser is tested against a captured production response. See
  ADR-0006.
- **Event identity is date + game number.** Start-time moves (rain delays,
  NFL flex) do not change identity; minute-level timestamps caused silent
  non-matches. Listings require a configured start; a feed event that
  shares a listing's teams and date but not its game number logs a drift
  warning and is never priced.
- **Net-edge gating.** The threshold applies to gross edge minus
  per-contract taker fee minus slippage, widened by fair-value uncertainty.
  A flat gross-edge threshold is wrong because Kalshi-style fees peak at 50c.
  The standard-error floor keeps single-book consensus from claiming false
  certainty.
- **Two-pass sizing.** Size tentatively on gross edge to learn the contract
  count (which sets the per-contract fee under per-order round-up), gate on
  net edge, then size finally on net edge, shrunk by uncertainty and capped
  by order-book depth. The final edge is computed at the fill's average
  price, not the top of the book.
- **Bitemporal observations.** Every quote carries `valid_at` (true at the
  venue: the Odds API market `last_update`) and `recorded_at` (seen by us).
  The store is append-only with stable outcome keys (never `repr`); `as_of`
  reconstructs what we knew at any moment for honest backtests and CLV.
- **Fail-closed execution.** Live brokers raise until their order paths are
  implemented, eligibility is verified (notably NY for Kalshi sports
  contracts and US geo-blocking for Polymarket Global), and live trading is
  explicitly authorized. Paper execution consumes its simulated liquidity so
  one book's depth cannot be refilled forever.

## Polling

Exchange books move every minute; sportsbook odds cost credits per call, so
they refresh on a slower cadence (`sportsbook_interval_s`); discovery only
feeds the human review queue and runs hourly. A sportsbook failure keeps the
previous prices instead of killing the tick. Listings beyond `horizon_days`
are not traded.

## Current state

Working: domain model, pricing (additive/multiplicative/power devig, terms
partitions, standard-error floor), fee-aware detection with the equal-terms
rule, bid-only Kalshi parsing (batch endpoint, fractional counts floored),
listing registry with terms-change quarantine, date+game-number event
identity, SQLite store with stable outcome keys, Discord notifier (async,
post-execution), dry-run/paper execution (paper consumes simulated
liquidity), service loop with separate sportsbook/discovery cadences,
horizon filtering.

Not yet: Shin devig, push-probability modeling (half-point lines accepted
as-is until then), combo/correlation detection, ladder-consistency
detectors, settlement feeds (daily loss limits need them), maker execution
(cancellation/inventory controls), DynamoDB/AWS deployment, closing-price
capture and CLV reporting.

## Decisions

Significant decisions are recorded as ADRs in `docs/adr/`. The betting
principles in `docs/principles.md` are the standard every change is checked
against; proposals that conflict get challenged before they get built.
