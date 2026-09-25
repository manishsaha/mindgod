# ADR-0009: MLB listed pitchers are an adjustable terms difference

Date: 2026-09-25
Status: Accepted. Supersedes the blanket MLB moneyline skip in `odds_api.py`. Implementation: complete.

## Context

Some sportsbooks settle MLB moneylines on listed pitchers: the bet voids if
either announced starter does not start. Others settle as "action," and the
rule varies by book. Neither the Odds API nor Kalshi reports which rule
applies, so the current adapter skips every MLB moneyline.

That skip removes `KXMLBGAME`, Kalshi's main MLB market, which is most of the
MLB half of our scope. The mismatch it protects against is small and can be
quantified:

- A listed-pitcher price is P(win | both announced starters pitch).
  Kalshi's price is the unconditional P(win).
- The gap is roughly P(late scratch) × (the win-probability swing from the
  scratch). Late scratches after probables are announced are uncommon, so the
  bias is typically a fraction of a point, well below `min_net_edge`.
- The real risk is the scratch itself, and it is detectable.

The skip also puts policy in an adapter. Adapters should report what they know
and let the application decide.

## Decision

1. **Represent the unknown in the domain.** `VoidPolicy` gets an explicit
   pitcher rule:

   ```python
   class PitcherRule(StrEnum):
       ACTION = "action"  # settles regardless of starters
       LISTED = "listed"  # voids unless listed_pitchers both start
       UNKNOWN = "unknown"  # the source does not say
   ```

   `listed_pitchers` keeps the pitcher IDs when the rule is `LISTED`.

2. **Adapters report, they don't decide.** The Odds API adapter emits MLB
   moneylines with `PitcherRule.UNKNOWN` instead of dropping them. Kalshi
   listings are `ACTION` unless the market's rules say otherwise.

3. **The application decides compatibility.** `terms_key` excludes the
   pitcher rule. A separate compatibility check runs at fair-value lookup:
   - equal rules: accept;
   - `UNKNOWN` or `LISTED` versus `ACTION`: accept, adding
     `pricing.pitcher_rule_se` (default 0.01) to the standard error in
     quadrature;
   - anything else: reject, and put the market in the review queue.

4. **Suppress on pitcher news.** A `ProbablePitcherSource` port (first
   adapter: the public MLB Stats API) records each game's probable starters.
   When a game's probables change, or when fewer than two are announced, its
   MLB listings are suppressed until a sportsbook refresh arrives with a quote
   whose valid_at is later than the change time, or until 10 minutes have
   passed since the change, whichever is later. A refresh that lands seconds
   after a scratch, before books have repriced, does not lift suppression.

## Consequences

- Most of MLB coverage comes back, at a small, explicit uncertainty cost.
- A scratch suppresses alerts instead of producing a fake edge from lines
  priced on the old starter.
- Adds one port and one adapter, plus a per-game probables table in the store.
- Revisit `pitcher_rule_se` once graded MLB calls (ADR-0008) show whether
  moneyline CLV differs by book.
