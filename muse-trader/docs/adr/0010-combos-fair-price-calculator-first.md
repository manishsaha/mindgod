# ADR-0010: Combos: fair-price calculator first, RFQ automation later

Date: 2026-09-25
Status: Proposed. Implementation: not started.

## Context

Correlated combos priced as if the legs were independent are the first edge
in `docs/principles.md` §4. Today only the domain types exist (`Combo`,
`ComboQuote`, `Combo.independent_probability()`). No detector, adapter, or
correlation model uses them.

How Kalshi combos work shapes the design:

- Combos (multivariate event, "MVE", markets) have no public order book. They
  are priced through request-for-quote (RFQ): the requester names the legs
  and size, market makers reply privately with Yes and No prices, the
  requester accepts, and the maker confirms.
- Kalshi classifies every combo market as high-volatility, with shorter
  accept/confirm windows.
- Creating RFQs and receiving quotes requires authenticated API access.

Two consequences follow. Seeing real combo prices programmatically means
putting trading-capable Kalshi credentials on the server, which ADR-0008's
watch-only posture avoids. And the hard part of the edge is not fetching
quotes; it is knowing the fair joint probability of correlated legs.

## Decision

Build combos in three phases. Each phase is gated on graded results from the
one before it.

### Phase 1: combo fair-price calculator (alert-first, no Kalshi credentials)

1. The user builds a combo in the Kalshi app and submits its legs to the
   service through a Discord command (`/combo leg1; leg2; ...`) or a small form.
2. The service resolves each leg to a canonical `Outcome` with the existing
   builders, prices it, and replies:

   > **Combo fair 23.4% (±1.8)** · take any quote **≤ 20¢** · legs: …
   > Correlation model: same-game table v1 · independent price would be 19.1%

3. The reply is recorded as a call (`detector = "combo_calculator"`), so the
   ADR-0008 grading loop covers combos from day one. The user logs the quote
   they took with the existing manual-fill path.

**Limit price.** The same rule as single-leg calls: the highest price at
which net edge after fees clears the threshold. The threshold is widened by
the combo's standard error, which includes correlation-model uncertainty.

**Rejects.** The calculator refuses, with a reason, when:
- a leg can't be resolved to a canonical outcome;
- a leg has no fair value, or the fair value's terms don't match the leg;
- the combo mixes periods or players in ways the correlation model doesn't
  cover.

### Phase 2: correlation model

Fair joint probability, in order of difficulty:

1. **Cross-game legs.** Treat them as independent: the product of each leg's
   devigged fair value. This already works through `independent_probability()`.
2. **Same-game legs: a correlation table (v1).** Pairwise adjustments for
   well-understood structures, estimated from historical game data and stored
   as versioned config:
   - favorite moneyline × that team's total over;
   - game total over × player yardage overs;
   - QB passing yards × his top receiver's receiving yards;
   - spread cover × game total, by side.

   Joint probability comes from a Gaussian copula over the legs' fair
   marginals, using the pairwise correlations. Correlation uncertainty goes
   into the standard error.
3. **Same-game legs: game simulation (v2).** A drive- or plate-appearance-
   level simulator produces the joint distribution directly. It replaces the
   table once graded combo calls show the table's errors matter.
4. **Benchmark.** Sportsbook same-game parlay prices, which carry heavy vig,
   are a generous upper bound when a data source provides them. A Kalshi
   combo quote that pays more than a book's same-game parlay on the same legs
   is almost certainly mispriced.

### Phase 3: RFQ automation (deferred)

Only once graded Phase 1 calls show positive closing-line value (CLV) for a
combo type:

- A `ComboQuoteSource` port with a Kalshi RFQ adapter that discovers eligible
  legs through the multivariate collections endpoint, creates RFQs, and
  listens on the authenticated communications WebSocket.
- **Credentials:** a dedicated API key. If Kalshi can't issue a key that can't
  trade, use a separate low-balance account. The key file is mounted
  read-only, and only its path is in the environment.
- **Per-maker cooldown tracking:** `ComboQuote.quoter_id` feeds a scheduler
  that spends scarce fills on the highest-EV combo first, not the first one
  found.
- **Courtesy limits:** cap how many RFQs go unanswered, to avoid spamming
  market makers with requests we never act on.

## Grading combos

- **Closing line.** Each leg's ADR-0008 closing line, combined through the
  same correlation model version that made the call. The model version is
  stored on the call, so a later model can be evaluated against the same
  closes without rewriting history.
- **Settlement.** From the Kalshi combo market's own result. Any voided leg
  follows Kalshi's combo rules; record the combo as void rather than guessing.
- **Reports** slice combo calls by leg count, same-game versus cross-game,
  and correlation structure.

## Consequences

- The biggest edge in the principles becomes reachable without trading
  credentials on the server.
- The correlation model is the long pole. Phase 1 is only as good as the
  table, which is why each call records its model version and the grading
  loop measures it.
- The Discord calculator is the first *inbound* interface (the user sends
  something to the service), so it needs its own port and a strict parser.
  A malformed leg must be rejected, never guessed at.

## Tests required

- Cross-game combo fair value equals the product of the leg fair values.
- A positively correlated same-game pair prices above the independent product;
  a negatively correlated pair prices below it.
- Limit price respects fees and the widened threshold.
- An unresolvable leg is rejected, and the rejection reason is returned.
- A combo call records its correlation model version, and grading reproduces
  the close from the legs' closes with that version.
