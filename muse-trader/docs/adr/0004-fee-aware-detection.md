# ADR-0004: Fee-Aware, Uncertainty-Aware Detection in the Application Layer

Date: 2026-09-25
Status: Accepted

## Context

The domain models the taker fee as a per-order, price-dependent function
(`QuadraticFeeModel` with per-order round-up) and fair value as a
probability plus standard error with lineage. The application layer had to
decide how to turn those into bet/don't-bet decisions and sizes.

## Decision

1. **Gate on net edge, not gross edge.** The threshold applies to gross
   edge minus per-contract taker fee minus slippage, and widens further by
   fair-value uncertainty (`min_net_edge + threshold_widening * se`). A
   flat gross-edge threshold is wrong because Kalshi-style fees peak at
   50c and shrink to the tails, so the same gross edge is bettable at 90c
   and unbettable at 50c.

2. **Two-pass sizing.** Size tentatively on gross edge to learn the
   contract count (which sets the effective per-contract fee under
   per-order round-up), gate on the resulting net edge, then size finally
   on net edge with fractional Kelly, shrunk by uncertainty
   (`1 / (1 + aversion * se)`), and capped by order-book depth. The final
   edge is computed at the fill's average price, not the top of book.

3. **Fair value carries error bars.** `WeightedConsensusModel` devigs
   each book-market with a pluggable method, weights books (sharp books
   configurable), and reports the weighted standard deviation of
   cross-book disagreement as the standard error. Books that disagree
   widen the gate and shrink the stake automatically.

4. **Explicit listing registration.** `RegistryResolver` is the source of
   truth for venue markets. Discovery finds tickers; the resolver maps
   them to canonical listings. Settlement/terms changes quarantine to a
   review queue instead of silently overwriting. Unregistered markets are
   never traded.

5. **Execution fails closed.** Dry-run and paper modes work out of the
   box. Live brokers raise until their order paths are implemented,
   eligibility verified, and live trading explicitly authorized.

## Consequences

- Tiny orders die at the net gate because round-up inflates their
  effective fee; this is intended, not a bug.
- Stakes scale with confidence: high disagreement means small or no bet.
- Backtests must use `as_of` from the append-only store so fair values
  are computed from what was known at the time.
