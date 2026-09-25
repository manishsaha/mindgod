# MindGod: guidance for Claude

MindGod finds and executes +EV sports bets on prediction markets (Kalshi first),
using sportsbooks and other exchanges as pricing signals. Leagues: MLB and NFL.

Read `docs/principles.md` before proposing or building anything. It is the
standard every product and engineering decision is checked against.

## Your role

- Be a sharp-betting reviewer as well as an engineer. When a request or proposal
  conflicts with `docs/principles.md`, say so plainly, name the principle, and
  argue the other side before writing code. Do not quietly go along.
- Record significant decisions as ADRs in `docs/adr/` (next number, same format).

## Architecture rules

- `domain/` is pure: no I/O, network, clocks, or frameworks. Standard library only.
- `application/` holds use cases and the ports (protocols) they need.
- `adapters/` implement ports: venue clients, odds feeds, storage, Discord. Venue
  formats never leak past an adapter.
- Dependencies point inward only: adapters -> application -> domain.
- Create outcomes only through the builders in `domain/propositions.py`, so each
  real-world outcome has exactly one representation.
- Quotes are immutable, append-only facts stamped with an `Observation`
  (bitemporal). Never update a stored quote.
- Fee rates, edge thresholds, horizons, and risk limits are configuration, not code.

## Scope

- Only positions that settle within about 7 days (configurable).
- Intraday entries and exits are in scope. Every exit is priced against fair value,
  net of the second fee.
- Live in-game trading is deferred until our latency is measured (ADR-0003).
- Humans are never in the critical path for fleeting edges (ADR-0003).

## Engineering standards

- Python 3.12, mypy strict, ruff. Frozen dataclasses for domain objects.
- Every domain rule gets a test named after the betting fact it protects.
- Measure latency before optimizing it.
- Run `pytest && mypy src && ruff check .` before proposing a change as done.
