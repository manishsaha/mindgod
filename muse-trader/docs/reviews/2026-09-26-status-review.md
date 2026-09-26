# Status review: `ebfdf17` (2026-09-26)

Adversarial review of `muse-trader` at `ebfdf17`, measured against
`docs/principles.md` and the ADRs. All gates pass: 138 tests with warnings
treated as errors, strict mypy, and ruff. This replaces the earlier review
of `922b73c`.

**Verdict: no known code blockers for the NFL week-4 shadow run.** What's
left is four checks on the first poll from your own host (below).

## Resolved in `922b73c..ebfdf17`

- **Zero calls, fixed (ADR-0011, now Accepted).** The NFL moneyline tie
  adjustment is implemented for live pricing and closing lines, with 332
  lines of tests. Verified end to end against the week-4 config: a
  −200/+170 book pair now produces a tie-adjusted fair value of 0.6482 for
  the Kalshi BUF listing, which previously had none.
- **Secrets fail loudly.** `ODDS_API_KEY`, `DISCORD_WEBHOOK_URL`,
  `DISCORD_WEBHOOK_OPS`, and `MINDGOD_DB_PATH` are required at startup. Soft
  startup requires the explicit `MINDGOD_SOFT_STARTUP=1`.
- **#ops is separate.** The watchdog refuses to fall back to #calls. After
  every poll, the service posts the Odds API credit used and remaining, plus
  the bookmakers returned, to #ops, and it posts poll failures there too.
- **Credit visibility.** Regions and markets are configurable
  (`ODDS_API_REGIONS`, `ODDS_API_MARKETS`), and the real per-poll cost is
  reported from the `x-requests-last` header.

## Correction to the previous review (B2, credit burn)

The earlier review assumed a cost of markets × regions = 2 credits per poll.
But the adapter sends a `bookmakers` parameter as well as `regions`. The
Odds API treats `bookmakers` as an alternative to regions, and those books
can come from any region. As I understand their v4 docs, when both are sent
the bookmakers list takes priority and each group of up to 10 books costs
the same as one region. With three books and h2h only, that's likely
**1 credit per poll** (about 438 for the weekend), which fits the quota. The
`x-requests-last` value posted to #ops after the first poll settles it.

Two consequences:

- `ODDS_API_REGIONS` is effectively ignored while `bookmakers` is set, and
  the comment "regions=us alone never returns Pinnacle" doesn't apply,
  because Pinnacle is requested by key.
- The book list is hard-coded to `draftkings,fanduel,pinnacle`. The
  `circa: 3.0` weight in `config.week4.yaml` never applies, and the
  consensus is at most three books. Make `bookmakers` configurable, and
  remove or fix the regions comment.

## First-poll checklist (your host, before 1 pm ET Sunday)

1. **Kalshi reachable:** the log shows `tick done`, with no 403s on the
   order-book batch request. The reviewer's sandbox is blocked from Kalshi,
   so this couldn't be checked here.
2. **Credit cost:** #ops shows `used 1` per poll. If it shows 2 or more,
   raise `sportsbook_interval_s` before Saturday night.
3. **Pinnacle:** the books list in #ops includes `pinnacle`. If it doesn't,
   the consensus is DraftKings plus FanDuel only. Note that before trusting
   the week-4 grades, and prioritize a second data source.
4. **Fair values exist:** within one sportsbook refresh, the log or DB shows
   fair values for the registered games. Zero opportunities is fine; zero
   *fair values* is a bug.

## Minor

- **Stringly-typed terms round trip.** `parse_terms_key` rebuilds Terms by
  splitting the partition key string. Return the Terms objects from
  `values_by_terms` instead (see ADR-0011's follow-up note).
- **Quota posts every poll** go to a muted channel, so that's acceptable.
  Add a louder #ops alert, and an automatic slowdown, when `remaining`
  drops below a reserve (say 100).
- **Closing-line retries never stop.** Started events that can never produce
  a close are retried every loop. Mark them terminally unavailable once the
  call has settled.
- **No-side flip condition.** The close flips on `listing.key.side == "no"`.
  Testing `basis != listing.outcome` would tie it to the domain rather than
  to the venue's side label. Behavior is equivalent today.

## Open work after the weekend

| Item | Where | Priority |
|---|---|---|
| Configurable `bookmakers`; second sportsbook adapter; compare coverage and freshness | — | High if Pinnacle is missing |
| Durable snapshot and reaction scheduling (lost on restart) | ADR-0008 | Medium |
| Same-game exposure in alerts; "Took it" button | ADR-0008 | Medium |
| Discord routing for #review-queue and #grades | — | Medium |
| Combo fair-price calculator | ADR-0010 | After the first graded batch |
| Automatic tier and RFQ automation | ADR-0003, ADR-0010 | Gated on graded CLV |

## Resolved earlier (cumulative)

- **Pricing:** Kalshi bids-only parsing (ADR-0006); terms travel with prices
  and the push sign is fixed (ADR-0005); devig happens before the terms
  partition, and lone sides are dropped.
- **Staleness:** gated on confirmation age; quadrature age term; directional
  move check with a spread gate (ADR-0007).
- **Grading:** reaction-time fills, decay snapshots, limit-price alerts,
  devigged side-correct closing lines through the public `consensus()`, and
  retryable per-loop grading with closing-line capture that fills in past
  events (ADR-0008).
- **Data:** MLB no-tie normal form; append-only method versioning and the
  recompute migration; reports on the current version only, with exclusions
  reported and a unique grade per version.
- **Config:** a YAML 1.2 boolean loader (`side: yes` and the Saints' `NO`
  stay strings), plus a startup test built from the committed config.
- **MLB:** the listed-pitcher rule is adjustable, with suppression on
  probable-pitcher changes (ADR-0009).
