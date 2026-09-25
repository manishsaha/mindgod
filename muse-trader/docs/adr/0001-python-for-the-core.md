# ADR-0001: Python for the core, with a replaceable hot path

**Status:** Accepted

## Context

Speed matters for a sharp bettor. But for this system, end-to-end latency is
dominated by network round trips and data-feed delay (milliseconds to seconds),
not by CPU time spent on our own logic (microseconds). The core also needs fast
iteration on quantitative models (devigging, correlation, simulation), where
Python's ecosystem is strongest.

## Decision

- Python 3.12 with asyncio. The domain is pure standard library.
- Streaming connections (WebSocket or similar) wherever a venue or feed offers
  them; polling only as a fallback.
- The hot path (receive a quote → detect an edge → place an order) sits behind
  application ports, so a single component can be rewritten in Rust or Go if
  profiling shows CPU on that path matters.

## Consequences

- Fast model iteration and one language across research and production.
- Deliberately not competing on microsecond latency. Edges that require that
  latency are out of scope unless measurement says otherwise.
