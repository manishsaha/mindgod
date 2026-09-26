# Status review: `922b73c` (2026-09-26)

Adversarial review of `muse-trader` at `922b73c`, measured against
`docs/principles.md` and the ADRs. All gates pass: 125 tests with warnings
treated as errors, strict mypy, and ruff. The service was started for real
with `deploy/config.week4.yaml`. It boots, builds all 15 listings, and ticks.
(Kalshi returned 403 from the reviewer's sandbox because of that sandbox's
network allowlist; this says nothing about the service. Confirm Kalshi
connectivity on your own host.)

## Blockers for the NFL week-4 shadow run (Sunday 2026-09-27)

### B1. The run still produces zero calls: NFL moneyline ties (ADR-0011)

All 15 listings are Kalshi NFL moneylines, which settle No on a tie. The
books' 2-way moneylines refund on a tie. The terms keys differ, so no listing
ever gets a fair value, and no closing line can form either. Reproduced at
`922b73c`: the book outcome matches the listing's outcome; `terms_key` does
not.

**Fix, either one:**
- Implement ADR-0011, the tie adjustment. It's small and unlocks the whole
  slate.
- Or register half-point spreads and totals for Sunday. If you do, set
  `ODDS_API_MARKETS` back to include `spreads,totals`.

Until then, every Odds API credit spent this weekend buys no calls.

### B2. The credit plan exhausts the quota mid-Sunday

`config.week4.yaml` budgets on "1 credit per market per poll": h2h only,
every 600 s, which comes to 438 credits over about 73 hours and fits a
500-credit quota. But the adapter requests `regions=us,eu`, and the Odds API
bills markets × regions. That's 2 credits per poll, 12 per hour, and about
**876 credits** for the weekend. At that rate a 500-credit quota runs out
about 42 hours after a Friday 11 pm start, around 5 pm ET Sunday: after the
early games kick off, before the late window and the night games.

**Fix, any of these:**
- Make regions configurable (`ODDS_API_REGIONS`) and run `us` only. Check
  first whether the `eu` region is what supplies Pinnacle; if you're on the
  free tier, it may not matter.
- Or poll only inside a window before each kickoff, say 6 hours. Saturday
  polling buys nothing for Sunday's closing lines.
- Either way, log the `x-requests-last` and `x-requests-remaining` headers
  every poll and send them to #ops, so the real burn rate is visible.

## Should fix before Sunday

- **Secrets still fail soft.** `test_no_odds_key_means_no_sportsbook` now
  *asserts* the soft behavior. In shadow mode, a missing `ODDS_API_KEY` or
  `DISCORD_WEBHOOK_URL` should stop the service at startup. Keep a soft mode
  only behind an explicit flag.
- **The heartbeat falls back to #calls** when `DISCORD_WEBHOOK_OPS` is unset.
  Set the ops webhook, or make the watchdog refuse to fall back.
- **Pinnacle presence is unverified.** Log which bookmaker keys come back on
  the first poll.

## Minor

- `capture_closing_lines` retries every started event without a close on
  every loop, forever. Events that can never pair (NFL moneylines today) are
  retried indefinitely. That's cheap now, but mark a close as terminally
  unavailable once the call has settled and the stored history can't produce
  one.

## Resolved in `1da8784..922b73c`

- **YAML booleans, fixed at the root.** The config loader uses YAML 1.2
  booleans, so `side: yes` stays a string and so does the Saints' team code
  `NO`, a second instance of the same bug. A new test builds the context from
  the committed week-4 config.
- **Retryable grading.** Settlement records facts only. Grading runs every
  loop, is safe to repeat, and retries on error. A settled call with no close
  is reported as "no close" instead of being graded with a CLV of None.
  Recorded in ADR-0008.
- **Closing-line capture has no one-hour window.** Late capture reads the same
  stored pre-kickoff history.
- **Startup duplicate check.** Duplicate grades now fail with a clear message
  instead of a bare `IntegrityError`.
- **Credit control.** `ODDS_API_MARKETS` and a 600 s interval, though see B2.

## Open work after the weekend

| Item | Where | Priority |
|---|---|---|
| NFL tie adjustment (if not done for B1) | ADR-0011 | High |
| Durable snapshot and reaction scheduling (lost on restart) | ADR-0008 | Medium |
| Same-game exposure in alerts; "Took it" button | ADR-0008 | Medium |
| Discord channel routing (#ops, #review-queue, #grades) | — | Medium |
| Second sportsbook adapter; compare coverage and freshness | — | Medium |
| Combo fair-price calculator | ADR-0010 | After the first graded batch |
| Automatic tier and RFQ automation | ADR-0003, ADR-0010 | Gated on graded CLV |

## Resolved earlier (cumulative)

- **Pricing:** Kalshi bids-only parsing (ADR-0006); terms travel with prices
  and the push sign is fixed (ADR-0005); devig happens before the terms
  partition, and lone sides are dropped.
- **Staleness:** gated on confirmation age; quadrature age term; directional
  move check with a spread gate (ADR-0007).
- **Grading:** reaction-time fills, decay snapshots, limit-price alerts, and
  devigged side-correct closing lines through the model's public
  `consensus()` (ADR-0008).
- **Data:** MLB no-tie normal form; append-only method versioning and the
  recompute migration; reports on the current version only, with exclusions
  reported and a unique grade per version.
- **MLB:** the listed-pitcher rule is adjustable, with suppression on
  probable-pitcher changes (ADR-0009).
