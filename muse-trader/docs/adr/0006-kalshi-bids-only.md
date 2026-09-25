# ADR-0006: Kalshi order books are bids only

Date: 2026-09-25
Status: accepted

## Context

Kalshi's `orderbook_fp` returns two ladders, `yes_dollars` and `no_dollars`,
and both are resting bids: ascending, best bid last, fixed-point dollar
strings with possibly fractional counts. There is no ask ladder. The old
parser treated yes bids as yes asks: yes bids at 45c/48c with a no bid at
50c reported a 48c yes ask when the real ask was 50c, a fake 5c edge.

The batch endpoint `GET /markets/orderbooks` takes repeated `tickers`
params (up to 100 per request); the comma-joined form silently returns
empty books.

## Decision

1. Parse both ladders as bids. For a yes listing, asks are `1 - no bids`
   (best first); for a no listing, asks are `1 - yes bids`.
2. Parse price and count as `Decimal`; floor fractional counts to whole
   contracts (a level flooring to zero is dropped). Never round up.
3. Keep the legacy `orderbook` integer-cents envelope as a fallback; an
   unrecognized payload is an empty book (no book, no trade).
4. Fetch with `/markets/orderbooks` in chunks of 100 repeated `tickers`
   params, with per-ticker fallback. `order_books` returns one book per
   listing in input order; a failed fetch yields an empty book so the
   pairing stays positional.

## Consequences

- Ask-side prices rise to their true values; any edge that depended on the
  old misread disappears (as it should).
- Flooring fractional liquidity is conservative: reported depth never
  exceeds what can actually be bought.
- Tests run against a captured production response
  (`tests/fixtures/kalshi_orderbook_fp.json`), including the 45/48/50
  regression.
