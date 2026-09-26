# ADR-0011: NFL moneylines: the tie refund is an adjustable terms difference

Date: 2026-09-25
Status: Accepted. Implementation: complete (`ebfdf17`). Verified end to end
against `deploy/config.week4.yaml`: a −200/+170 book pair produces a
tie-adjusted fair value (0.6482, method `power-devig+tie-adj`) for the
Kalshi BUF moneyline listing, which previously had none.

## Context

- A sportsbook's 2-way NFL moneyline refunds the stake on a tie. Once the vig
  is removed, its price is P(team wins | no tie).
- Kalshi's "team wins" contract settles No on a tie. Its price is the
  unconditional P(team wins).

Under ADR-0005's equal-terms rule these never match, so every NFL moneyline
listing gets no fair value and produces no calls. That fails safe, but it
shuts off Kalshi's most liquid NFL market. The same gap blocks closing lines:
the complement of Kalshi's "home wins" is "home margin ≤ 0," while the book's
away moneyline is "home margin ≤ −1," so the complement pair never forms.

The difference is a single, well-understood quantity: the tie probability.
ADR-0009 already treats the MLB pitcher rule as an adjustable difference;
this applies the same pattern.

## Decision

1. **Convert instead of rejecting.** When the book's terms refund on
   `margin == 0` and the listing's terms lose on a tie (NFL, `FULL_GAME`,
   `MARGIN`), convert with the tie probability *t*:

   ```
   P(home wins)         = P_book(home) × (1 − t)
   P(away wins)         = P_book(away) × (1 − t)
   P(home margin ≤ 0)   = P(away wins) + t        # Kalshi "home wins" No side
   ```

   The converted values sum to 1 over {home win, away win, tie}.

2. **Configure the tie rate.** `pricing.nfl_tie_prob` defaults to 0.004, a
   conservative estimate of the modern-overtime tie rate. Recalibrate it
   each season from results.

3. **Add its uncertainty.** `pricing.nfl_tie_prob_se` (default 0.003) is
   added to the fair value's standard error in quadrature, as in ADR-0007.

4. **Closing lines** use the same conversion. The complement pairing accepts
   the book's "home margin ≤ −1" as the counterpart of "home margin ≥ 1"
   *only* under this rule, and the tie is added back when converting to the
   listing's No side.

5. **Where it lives.** This is a pricing-layer rule in `application/`, next
   to the pitcher rule, keyed on `(league, period, stat, terms difference)`.
   Adapters keep reporting terms as they are; they don't adjust anything.

## Consequences

- NFL moneylines become priceable, which is the largest NFL coverage gain
  available.
- At even odds the adjustment is a fraction of a point. That's too small to
  create a false edge by itself, but without it Yes-side fair values carry a
  consistent upward bias.
- If the tie rate is badly wrong, errors are small and one-directional, and
  graded moneyline calls will show them.

## Tests required

- −150/+130: after conversion, home > away, and home + away + t = 1.
- Kalshi "home wins" Yes and No fair values sum to 1, with the tie on the No
  side.
- The conversion never touches spreads, totals, MLB, or non-`FULL_GAME`
  periods.
- The closing-line pairing forms for an NFL moneyline under the rule and
  produces the converted value.

## Implementation notes (`ebfdf17`)

- `application/tie.py` holds the rule. `find_tie_adjusted_fair` is the live
  path, and `tie_close_basis` / `tie_aware_counterpart` handle closing lines.
- Closing lines pair the yes basis with the book's "home margin ≤ −1" only
  under this rule, convert with (1 − t), and flip once for No-side listings.
- `pricing.nfl_tie_prob` and `pricing.nfl_tie_prob_se` are config values on
  the model.
- **Follow-up (done):** `values_by_terms` now returns the `Terms` for each
  partition alongside its fair values, so the rule reads the book's refund
  terms from the object instead of parsing the `terms_key` string back.
  `parse_terms_key` is removed.
