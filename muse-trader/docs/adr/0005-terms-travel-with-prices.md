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
   Devigging runs per market group *before* partitioning, so both sides of
   a market are always devigged together no matter what their terms look
   like; a terms bug can never silently switch the vig removal off. A
   lone side is dropped, never passed through: one price carries the full
   overround (see the 104.8% incident below).
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

## The 104.8% incident (2026-09-25)

The spread push was built as `margin_exactly(team, abs(point))`. For the
favorite that is right (KC -3 pushes when KC wins by 3), but for the
underdog it described a different game: BUF +3 was recorded as pushing
when BUF wins by 3, instead of when KC wins by 3. The two sides got
different refund terms, `partition_by_terms` split the pair, and each lone
side passed devigging through untouched: 52.38% + 52.38% = 104.8%, a
built-in 2.4-point phantom edge from the vig alone.

Fixed as `margin_exactly(team, -point)`, which canonicalizes (via the
domain's home-minus-away normalization) to one refund outcome for both
sides. The devig-before-partition ordering plus the lone-side drop mean
this bug class now fails closed even if the terms are ever wrong again:
split sides are dropped, never passed through with the vig intact.
Regression: `test_underdog_push_terms_match_favorite`.
