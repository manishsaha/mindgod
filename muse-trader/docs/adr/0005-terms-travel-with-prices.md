# ADR-0005: Settlement terms travel with every price

Date: 2026-09-25
Status: accepted

## Context

KC -3 at -110 on both sides devigs to 50%. The canonical outcome is
margin >= 4, but that 50% is conditional on no push at exactly 3. With
roughly 9% push probability the unconditional probability is about 45.5%.
A Kalshi ask at 47c then looks like +2.5c edge when it is really about
-1.5c. NFL moneyline tie refunds and whole-number totals have the same
structure: the canonical outcome matches, the bet does not.

`PricedOutcome` used to drop payout terms entirely, and the complement
helper inherited the bad fair value into no-side listings.

## Decision

1. `PricedOutcome` carries `Terms`. The Odds API adapter builds them:
   whole-number spreads refund at exactly the line, whole-number totals
   refund at exactly the total, NFL moneylines refund ties, half-point
   lines carry no refund.
2. The consensus model devigs inside terms partitions (`partition_by_terms`);
   a push-refunding line never informs a no-push line on the same outcome.
3. A listing uses a fair value only when `differences(book_terms,
   listing_terms)` is empty, checked at lookup time on the yes-basis
   outcome (refund rules are side-independent).
4. The listing registry builds the same refund terms from the spec
   (`build_terms`), so a registered whole-number spread can match its feed
   prices instead of never matching at all.

Until push probability is modeled, half-point lines are accepted as-is;
whole-number push-refunding lines are never treated as equivalent to them.

## Consequences

- Whole-number lines with no matching-terms feed simply do not trade; the
  failure is visible (no fair value) rather than a phantom edge.
- Feed/listings terms drift (a book changing its push rule) surfaces as a
  missing fair value, not a misprice.
- Tests: KC -3 regression (`test_no_fair_value_when_terms_differ`,
  `test_matching_terms_flow_through`), partition separation, and feed
  terms parsing in `tests/test_service.py`.
