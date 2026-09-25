# ADR-0007: Stale-quote defenses

Date: 2026-09-25
Status: Accepted (revised 2026-09-25, see "Revision" below)

## Context

Sportsbook prices refresh every 5 minutes and survive refresh failures; the
exchange book is at most 60 seconds old. When news breaks, the exchange
reprices in seconds while the consensus lags by minutes, and the system flags
the correct new price as mispriced. That is the stale-quote edge in reverse:
we are the stale side. Alerts are the product (ADR-0008), so a fake edge is
worse than a missed one.

## Decision

1. **Freshness gate** (`pricing.max_quote_age_s`, default 900). Quote age is
   **confirmation age**: `now - recorded_at`, the time since we last saw the
   quote as the book's current price. Quotes older than the gate are dropped
   before devigging and logged. A market group left with a lone side is then
   dropped by the lone-side rule, so a stale book never becomes a fair value.

   `valid_at` (the feed's `last_update`) is not used for gating. If it means
   "last changed," a line that holds steady looks old, and gating on it would
   drop the most settled lines. It stays in the store for feed-latency
   analytics and the future stale-quote detector.

2. **Age-widened uncertainty** (`pricing.stale_se_per_minute`, default 0.001).
   The age term is combined with the other uncertainty sources, not maxed
   against them:

   ```
   base = max(cross_book_std, min_standard_error)
   se   = sqrt(base² + (max_confirmation_age_min × stale_se_per_minute)²)
   ```

   Taking the max made the age term inert: at the 900 s gate it reaches 0.015,
   which never exceeds the 0.02 floor.

3. **Move check** (`engine.max_kalshi_move`, default 0.03).
   - *Anchor:* on each sportsbook refresh, snapshot the exchange mid per
     listing on the first tick after the refresh.
   - *Directional:* suppress only when the mid has moved *away* from our fair
     value by more than the threshold, i.e. the move widened the apparent edge.
     A move toward fair value shrinks the edge on its own and is not evidence
     of a stale consensus.
   - *Spread gate:* skip the check (and log) when the exchange spread is wider
     than `engine.move_check_max_spread` (default 0.06). On thin books the mid
     jumps with single orders and would cause false suppressions.
   - Fails open when there is no anchor yet.

## Consequences

- Genuine edges that appear while the consensus is stale are missed rather
  than mispriced. That trade is intended: the alert stream stays trustworthy
  at the cost of some recall during fast markets.
- Known gap: the anchor is taken when our refresh lands, not when the book
  prices were valid. An exchange move between those two moments is invisible
  to the check. Closing it needs anchors from the stored exchange history at
  each quote's `valid_at`.

## Tests required

- A test using production config values (`min_standard_error: 0.02`) proving
  the age term changes the standard error. Tests built on constructor
  defaults hid the inert-age-term bug.
- Confirmation-age gate: a quote with an old `valid_at` but a fresh
  `recorded_at` is kept; a quote whose last confirmation is past the gate is
  dropped.
- Move check: suppresses a move away from fair value, allows a move toward it,
  and skips when the spread exceeds the gate.

## Revision (2026-09-25)

- Gate on confirmation age instead of `valid_at`.
- Combine the age term in quadrature instead of `max()`.
- Make the move check directional and add the spread gate.

Open check: log `recorded_at - valid_at` for Pinnacle NFL main lines over a
quiet weekday. Values routinely above 15 minutes confirm that `last_update`
means "last changed."
