# ADR-0008: Alert-first; manual execution graded by paper tracking

Date: 2026-09-25
Status: Accepted. Amends ADR-0003.

## Context

The current goal is to call out sharp bets, not to automate execution. The
user takes calls by hand when they choose to, and every call is tracked on
paper so we can measure accuracy and find what to improve.

A human in the loop changes three things:

1. **Fleeting edges are out of reach.** Stale quotes and internal
   inconsistencies often close in seconds, before a person can act.
2. **Paper fills at the alert price would lie.** They would credit edges the
   user could never have taken, and the evaluation loop would be fiction.
3. **The alert is the product.** Its content, latency, and trustworthiness
   matter more than execution plumbing.

## Decision

### 1. Detectors target edges that last minutes, not seconds

Priority order: pre-game mispricing against sharp consensus, slower-moving
props, combos. Stale-quote sniping and internal arbitrage move to the
Automatic tier, which is deferred (ADR-0003).

### 2. Alerts state a price limit, not a price

Each alert says:

> **Buy up to N contracts of <outcome> at ≤ X¢**
> Fair value P (±SE) · net edge E at the current ask · book depth D at ≤ X¢
> Same-game exposure: $Y real, $Z paper · expires ~T

- **X, the limit price:** the highest ask at which net edge after fees and
  slippage still clears the (uncertainty-widened) threshold, computed with the
  existing fee model. The user can act on it without redoing any math.
- **N:** Kelly-sized contracts, capped by the depth available at ≤ X.
- **Same-game exposure:** positions on one game are one correlated bet. The
  user is the risk policy now, so the alert has to show it.
- Deduplicate by canonical outcome across venues, and re-alert only when X
  moves materially. Alert fatigue destroys the tool's value.

### 3. Paper fills model human reaction time

- **Reaction fill:** each call records a paper fill against the order book
  observed `reaction_delay_s` after the alert (default 60 s, configurable),
  walking the ladder up to X. If the edge is gone by then, it is recorded as
  "no fill," which is itself a result.
- **Decay snapshots:** book snapshots at +0, +30 s, +2 min, and +10 min after
  the alert. Together they give each call a decay curve, which shows which
  edge types can actually be taken by hand.
- **Instant fill (reference only):** a fill at the alert-time price is kept to
  measure how much edge the reaction delay costs. It never feeds headline
  metrics.

### 4. Manual fills are logged against the call

A Discord button on each alert, "Took it," asks for price and contracts and
writes a `ManualFill` linked to the call. Comparing manual fills with paper
fills measures the user's real execution slippage.

### 5. Every call is graded

Grading runs after the closing line and again after settlement (see the data
model below). The headline metric is closing line value (CLV) against the
sharp close, for reaction fills and manual fills. Profit and loss is
reported, but it is noise over short samples and never drives decisions.

## Data model

All tables are append-only. IDs are opaque strings.

| Table | Key fields | Written when |
|---|---|---|
| `calls` | call_id, created_at, listing_key, outcome_key, detector, fair_prob, fair_se, fair_sources, limit_price_x, contracts_n, ask_at_alert, net_edge_at_alert, event_start, same_game_exposure | Alert fires |
| `call_snapshots` | call_id, offset_s (0/30/120/600), best_ask, best_bid, depth_at_x, recorded_at | Scheduled after the alert |
| `paper_fills` | call_id, kind (`reaction` \| `instant`), contracts, avg_price, fee, filled (bool) | At +0 and +`reaction_delay_s` |
| `manual_fills` | call_id, contracts, price, fee, taken_at, note | User taps "Took it" |
| `closing_lines` | outcome_key, sharp_close_prob, source, captured_at | At event start (last sharp consensus before lock) |
| `settlements` | outcome_key, result (win/loss/refund/void), settled_at | Settlement feed |
| `call_grades` | call_id, clv_reaction, clv_manual, pnl_reaction, pnl_manual, edge_half_life_s | After close, updated after settlement |

Definitions:
- `clv = sharp_close_prob - fill_avg_price - fee_per_contract` (per contract,
  in probability points).
- `edge_half_life_s`: time until the ask at the snapshots crosses halfway from
  the alert ask to X. It is estimated from the snapshots.

Reports slice grades by detector, league, market type, price bucket
(0–20¢, 20–40¢, …), net edge at alert, fair-value SE, time to event start,
and the book that anchored fair value. That breakdown is the evaluation loop:
it shows where the edge is real and where the model is fooling itself.

## Consequences

- Recall drops on fast edges, deliberately. Precision and honesty of the
  grades go up.
- The store gains a scheduler duty (snapshots at fixed offsets) and a
  closing-line capture job at each event start.
- The Automatic tier becomes a data-driven decision: we'll enable it for an
  edge type only when its decay curves show that edges survive long enough to
  capture automatically but not manually.
- `PaperExecution`'s instant fill is demoted to a reference measurement.
