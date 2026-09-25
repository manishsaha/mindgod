# ADR-0003: Latency tiers; Discord is never in the critical path

**Status:** Accepted

## Context

We want real-time Discord notifications. But a notification that waits for a
human tap takes anywhere from about 10 seconds to several minutes. Stale-quote
and internal-arbitrage edges often close in seconds. Routing those through a
human would drop exactly the edges that matter most.

## Decision

Opportunities are assigned to execution tiers by expected edge lifetime:

| Tier | Edges | Execution | Discord's role |
|---|---|---|---|
| Automatic | Stale quotes, internal arbitrage | Automatic, within hard risk limits | Notification after the trade |
| Approve | Combos, slower-moving props | One-tap approve button with an expiry | Decision point |
| Digest | Research signals, market-maker behavior patterns | None | Summary |

Rollout:
1. **Shadow mode first.** Record what we would have traded and measure CLV
   before any real money moves.
2. **Automatic tier second,** with small limits.

Deployment runs in the AWS region with the lowest measured latency to each
venue's API. Candidate regions are chosen by latency tests, not assumptions.

## Consequences

- Discord outages or slow taps cannot cost us fleeting edges.
- Risk limits and kill switches must exist before the automatic tier goes live.
