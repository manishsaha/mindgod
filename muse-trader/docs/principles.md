# MindGod principles

These principles are the standard for every product and engineering decision.
Proposals that conflict with them get challenged before they get built.

## 1. We trade against participants, not a house

Kalshi prices are resting orders from market makers (MMs), retail, and other
sharps. Edge means identifying which participant is wrong, why, and whether the
error repeats.

There are no limits on winners, but order-book depth caps size, so expected value
is a function of size. Price the whole ladder, never just the top of the book.

MM defenses (wider spreads, pulled quotes, cooldowns on fills) are signals that
their pricing on that product is exploitable.

## 2. Fair value comes from sharp consensus

- Fair probability comes from removing the vig ("devigging") from the sharpest
  sources: Pinnacle, Circa, BookMaker, and liquid exchange prices. Sources are
  weighted by measured predictive power, not reputation.
- Devig methods (multiplicative, power, Shin, and others) are pluggable
  strategies, chosen empirically per sport and market type.
- Every fair value carries an uncertainty estimate and its lineage (the quotes it
  came from). Uncertainty widens edge thresholds and shrinks size.
- Closing Line Value (CLV) against the sharp closing price is the main metric.
  Short-run profit and loss is noise.

## 3. Expected value accounts for fees and size

- Kalshi-style fees have the form `ceil(rate × contracts × P × (1 − P))` per
  order and peak at 50¢. The break-even edge therefore depends on price, and a
  flat edge threshold is wrong.
- Rounding per order penalizes small orders.
- Fee rates live in configuration.
- Opportunities are ranked by return on capital per unit of time.

## 4. Where the edge comes from

1. **Correlated combos priced as if the legs were independent.** Sportsbook
   same-game parlay prices carry heavy vig, so they are a generous benchmark. A
   Kalshi combo that pays more than a sportsbook same-game parlay on the same legs
   is almost certainly mispriced. When an MM enforces cooldowns, fills are scarce:
   spend them on the highest-EV combo, not the first one found.
2. **Stale quotes** after news (injuries, lineups, weather) or sharp line moves.
3. **Internal inconsistencies:** strike ladders out of order, mutually exclusive
   outcomes whose prices don't sum to one, sub-markets inconsistent with the main
   line.
4. **Retail flow biases** (favorite–longshot bias, popular teams). These are
   usually exploited by taking the other side.
5. **Settlement-rule mismatches.** Treat these as a trap first and an occasional
   edge second.

## 5. Arbitrage is supplementary

Cross-venue arbitrage requires identical settlement terms, fee-adjusted prices,
simultaneous execution, and capital on both venues. Sportsbook accounts get
limited, so they are a resource that runs out. Use sportsbooks mainly as data and
take one-sided +EV positions on exchanges.

## 6. Execution and sizing

- Take liquidity (taker orders) for fleeting edges. Post resting orders (maker)
  only once we have fast cancellation and inventory limits; that is a later phase.
- Size with fractional Kelly, shrunk further by fair-value uncertainty, at the
  portfolio level. Positions on the same game are one correlated bet.

## 7. Horizon: settle within a week, trade within the day

- Only positions that settle within about 7 days. This is configuration, not code.
- Intraday entries and exits are in scope, but every exit is priced against fair
  value. Sell when the bid exceeds fair value plus the exit fee, or when freeing
  the capital for a better opportunity is worth more than the second fee. An exit
  without a fair-value reason is momentum trading and is out of bounds.
- A round trip pays two fees. Liquidity on the exit side is part of the entry
  decision.
- Live in-game trading is deferred until our latency has been measured against
  the market makers'.

## 8. Current phase: alerts first, graded honestly

- The product is the call, not the execution. The user takes calls by hand;
  every call is tracked on paper and graded (ADR-0008).
- Paper fills model human reaction time. A paper result the user could not
  have achieved is worse than no result.
- Target edges that last minutes. Edges that close in seconds wait for the
  Automatic tier, which is enabled per edge type only when graded decay data
  justifies it.
- A fake edge is worse than a missed one. When in doubt (stale consensus,
  unproven terms, pitcher news), suppress the alert.

## 9. Speed is measured, then engineered

- Latency is dominated by network and feed delay, not CPU. Use streaming over
  polling wherever a venue offers it, run close to the venues, and measure feed
  latency per venue continuously.
- Humans are not in the critical path for fleeting edges (ADR-0003).

## 10. Data discipline

- Quotes are immutable, append-only facts with two timestamps (when valid at the
  venue, when we recorded them). We can always reconstruct what we knew at any
  moment.
- Use licensed odds feeds; don't scrape sportsbooks.
- Confirm the legal status of trading these contracts from our jurisdiction.
