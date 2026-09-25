# ADR-0002: One canonical outcome shape for every market

**Status:** Accepted

## Context

Venues describe the same bet in different ways: "KC −3", "KC wins by more than
3.5", "BUF +3.5". Matching them, checking that ladders are consistent, and
reasoning about pushes all need a single representation.

## Decision

- Every price maps onto an `Outcome`: a condition on a `Quantity` (event, stat,
  period, team, player).
- Moneylines and spreads are thresholds on one game-level quantity, `MARGIN`
  (home minus away). Totals and props are thresholds on their stat.
- Conditions on integer stats are normalized to integer bounds at construction.
- Settlement differences (refunds, voids, listed pitchers) live in `Terms`,
  separate from the Outcome.

## Alternatives rejected

A class per market type (Moneyline, Spread, Total, Prop) would need matching
logic for every pair of types and couldn't express "over 50.5 implies over
44.5" or "−3 and −3.5 differ only by the push".

## Consequences

- Equality means "same outcome"; cross-venue matching is a key lookup.
- Adapters carry the burden of parsing each venue's rules into `Terms`.
- New market shapes (such as "first team to score") need new Stats or
  Conditions, not new top-level types.
