# ADR-0007: Stale-quote defenses

Date: 2026-09-25
Status: accepted

## Context

Sportsbook prices refresh every 5 minutes and survive refresh failures
indefinitely; the exchange book is at most 60 seconds old. When news breaks,
the exchange reprices in seconds while the consensus lags by minutes, and
the system flags the correct new price as mispriced. That is the
stale-quote edge in reverse: we are the stale side. Now that alerts are the
product, a fake edge is worse than a missed one.

## Decision

1. Freshness gate (`pricing.max_quote_age_s`, default 900): quotes older
   than the gate are dropped before devigging and logged. A market group
   left with a lone side is then dropped by the lone-side rule, so a stale
   book can never become a fair value.
2. Age-widened uncertainty (`pricing.stale_se_per_minute`, default 0.001):
   the fair value's standard error is at least the max quote age in minutes
   times this rate, so an aging consensus is penalized before it is dropped.
3. Move check (`engine.max_kalshi_move`, default 0.03): on each sportsbook
   refresh the service snapshots the exchange mid per listing; an
   opportunity is suppressed when the mid moved more than the threshold
   since the refresh. The anchor is taken on the first tick after the
   refresh, and the check fails open when there is no anchor yet.

## Consequences

Genuine edges that appear while the consensus is stale are missed rather
than mispriced. That is the intended trade: the alert stream stays
trustworthy at the cost of some recall during fast markets.
