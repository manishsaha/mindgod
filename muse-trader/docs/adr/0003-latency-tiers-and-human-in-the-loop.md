# ADR-0003: Latency tiers; Discord is never in the critical path

**Status:** Accepted. Amended by ADR-0008: the Automatic tier is deferred, and
the current phase runs Approve and Digest only.

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
| Approve | Combos, slower-moving props, pre-game mispricings | Manual, by the user | Decision point |
| Digest | Research signals, market-maker behavior patterns | None | Summary |

Rollout:
1. **Shadow mode first.** Record what we would have traded and measure CLV
   before any real money moves.
2. **Automatic tier later,** with small limits, only once shadow-mode data
   shows which edges are real and how fast they close (ADR-0008).

Deployment runs in the AWS region with the lowest measured latency to each
venue's API. Candidate regions are chosen by latency tests, not assumptions.

## Consequences

- Discord outages or slow taps cannot cost us fleeting edges, because
  fleeting edges are not traded by hand in the current phase at all.
- Risk limits and kill switches must exist before the Automatic tier goes live.

## Amendment history

- 2026-09-25: Automatic tier deferred by ADR-0008. Alerts are the product;
  the user executes manually, and every call is graded through paper tracking.
