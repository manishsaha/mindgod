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
OddsApiSource.priced_outcomes()      sportsbook prices -> PricedOutcome
        |
WeightedConsensusModel.value()       devig per book-market, weight books
        |                            -> dict[Outcome, FairValue]
KalshiExchange / PolymarketExchange  order books for registered listings
        |
RegistryResolver                     ticker -> Listing (explicit table);
                                     unknown -> review queue, never traded
        |
ValueDetector.detect()               net-edge gate + fractional Kelly +
                                     uncertainty shrink + depth cap
        |
ExposureLimits -> Discord -> execution (dry-run | paper | live-fails-closed)
```

## Key design points

- **Canonical outcomes.** "KC -3", "KC wins by more than 3.5", and
  "BUF +3.5" all reduce to thresholds on home-minus-away margin with integer
  normal form, so equality means "same outcome". Settlement differences
  (push refunds, listed pitchers, voids) live in `Terms`, separate from the
  outcome. Two listings are the same bet only when both match.
- **Net-edge gating.** The threshold applies to gross edge minus
  per-contract taker fee minus slippage, widened by fair-value uncertainty.
  A flat gross-edge threshold is wrong because Kalshi-style fees peak at 50c.
- **Two-pass sizing.** Size tentatively on gross edge to learn the contract
  count (which sets the per-contract fee under per-order round-up), gate on
  net edge, then size finally on net edge, shrunk by uncertainty and capped
  by order-book depth. The final edge is computed at the fill's average
  price, not the top of the book.
- **Bitemporal observations.** Every quote carries `valid_at` (true at the
  venue) and `recorded_at` (seen by us). The store is append-only; `as_of`
  reconstructs what we knew at any moment for honest backtests and CLV.
- **Fail-closed execution.** Live brokers raise until their order paths are
  implemented, eligibility is verified (notably NY for Kalshi sports
  contracts and US geo-blocking for Polymarket Global), and live trading is
  explicitly authorized.

## Current state

Working: domain model, pricing (additive/multiplicative/power devig),
fee-aware detection, listing registry with terms-change quarantine, SQLite
store, Discord notifier, dry-run/paper execution, service loop.

Not yet: Shin devig, combo/correlation detection, ladder-consistency
detectors, settlement feeds (daily loss limits need them), maker execution
(cancellation/inventory controls), DynamoDB/AWS deployment, closing-price
capture and CLV reporting.

## Decisions

Significant decisions are recorded as ADRs in `docs/adr/`. The betting
principles in `docs/principles.md` are the standard every change is checked
against; proposals that conflict get challenged before they get built.
