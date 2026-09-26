"""One-shot recompute migration for the ADR-0008 grading loop.

Recomputes closing lines and call grades with the current (v2) method for
everything stored so far, append-only:

- Old v1 rows are never overwritten or deleted. New rows carry
  method_version=2; readers take the latest version present.
- Outcomes stored under an older domain normalization (MLB moneyline No
  sides as "margin <= 0" before the full-game no-tie rule) are re-keyed
  through the outcome_key_remap table, so those calls join to their new
  closes instead of silently staying ungraded.
- Idempotent: re-running skips outcomes that already have a v2 close and
  calls that already have a v2 grade.
- Dry-run (default) writes no data rows; it prints old vs new closing
  value per call, counts of calls that still can't be graded by reason,
  and the vig-removal sanity check.

Usage:
    python -m mindgod.application.recompute --db mindgod.db
    python -m mindgod.application.recompute --db mindgod.db --apply

Always run the dry run against a copy of the database first.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from mindgod.domain.propositions import Outcome

from .calls import (
    CALL_GRADE_METHOD_VERSION,
    CLOSING_LINE_METHOD_VERSION,
    ClosingLine,
    Settlement,
)
from .ports import FairValueModel, ObservationStore
from .pricing import outcome_key, parse_outcome_key
from .service import _close_for_outcome, _grade_settled_call


@dataclass(frozen=True, slots=True)
class CallRecompute:
    """What the migration did (or would do) for one call."""

    call_id: str
    market_id: str
    side: str
    stored_key: str
    canonical_key: str
    remapped: bool
    old_close: float | None
    new_close: float | None
    close_status: str  # recomputed | would_recompute | already_current | failed:<reason>
    old_clv: float | None  # v1 grade clv_reaction
    new_clv: float | None  # v2 CLV (read back, or old + close delta in dry-run)
    settled: bool
    grade_status: str  # graded | would_grade | already_graded | no_settlement


@dataclass(frozen=True, slots=True)
class SanityResult:
    """Vig-removal check: on -110-style markets the avg CLV should drop ~2.4pts."""

    status: str  # "pass" | "fail" | "insufficient_data"
    n: int
    avg_old_clv: float | None
    avg_new_clv: float | None
    drop: float | None  # avg(old_clv - new_clv), expected ~0.024


@dataclass(frozen=True, slots=True)
class RecomputeReport:
    dry_run: bool
    calls: list[CallRecompute] = field(default_factory=list)
    remaps: list[tuple[str, str]] = field(default_factory=list)
    sanity: SanityResult | None = None


# -110 implies 0.5238; the vig-included v1 close on a -110/-110 market sits
# near 0.524 while the devigged v2 close sits near 0.50, so CLV should drop
# about 2.4 points. The band admits slightly off -110 markets; the
# tolerance admits rounding and small quote differences.
VIG_CHECK_BAND = (0.51, 0.54)
VIG_DROP_EXPECTED = 0.024
VIG_DROP_TOLERANCE = 0.012
VIG_CHECK_MIN_N = 5


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def recompute_closes_and_grades(
    store: ObservationStore,
    model: FairValueModel,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> RecomputeReport:
    """Recompute v2 closing lines and grades for all stored calls.

    Dry-run writes no data rows (opening the DB may still add the version
    columns to the schema, which touches no data). Apply mode appends v2
    closes, v2 grades, and key remaps; re-running either mode is a no-op
    for work already done.
    """
    now = now or datetime.now(UTC)
    calls = store.all_calls()
    settled_by_call = {s["call_id"]: s for s in store.settled_calls()}

    # Pass 1: re-key. Parse each stored outcome and re-normalize through
    # the current domain rules; distinct keys that moved get a remap row.
    canonical_of: dict[str, tuple[Outcome | None, str]] = {}
    remaps: list[tuple[str, str]] = []
    for c in calls:
        stored = c["outcome"]
        if stored not in canonical_of:
            outcome = parse_outcome_key(stored)
            canonical = outcome_key(outcome) if outcome is not None else stored
            canonical_of[stored] = (outcome, canonical)
            if canonical != stored:
                remaps.append((stored, canonical))
                if not dry_run:
                    store.record_key_remap(stored, canonical, "mlb_no_tie_normalization", now)

    # Pass 2: closing lines, one per distinct canonical outcome.
    new_close_of: dict[str, float | None] = {}
    close_status_of: dict[str, str] = {}
    old_close_of: dict[str, float | None] = {}
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        _, canonical = canonical_of[c["outcome"]]
        by_canonical.setdefault(canonical, []).append(c)

    for canonical, group in by_canonical.items():
        stored_keys = {c["outcome"] for c in group}
        old_close = None
        for k in list(stored_keys) + [canonical]:
            row = store.get_closing_line_version(k, 1)
            if row is not None:
                old_close = row["sharp_close_prob"]
                break
        old_close_of[canonical] = old_close

        existing_v2 = store.get_closing_line_version(canonical, CLOSING_LINE_METHOD_VERSION)
        if existing_v2 is not None:
            new_close_of[canonical] = existing_v2["sharp_close_prob"]
            close_status_of[canonical] = "already_current"
            continue

        outcome, _ = canonical_of[group[0]["outcome"]]
        event_start = None
        for c in group:
            event_start = _parse_ts(c["event_start"])
            if event_start is not None:
                break
        if outcome is None:
            close_status_of[canonical] = "failed:unparseable_key"
            new_close_of[canonical] = None
            continue
        if event_start is None:
            close_status_of[canonical] = "failed:no_event_start"
            new_close_of[canonical] = None
            continue

        prob, reason = _close_for_outcome(store, model, outcome, event_start)
        if prob is None:
            close_status_of[canonical] = f"failed:{reason}"
            new_close_of[canonical] = None
            continue
        if not dry_run:
            store.record_closing_line(
                ClosingLine(
                    outcome_key=canonical,
                    sharp_close_prob=prob,
                    source="pregame_consensus_devigged",
                    captured_at=now,
                )
            )
        new_close_of[canonical] = prob
        close_status_of[canonical] = "recomputed" if not dry_run else "would_recompute"

    # Pass 3: grades for settled calls, through the production grading path
    # with the close pinned to v2 (a v2 grade never silently mixes in a v1
    # close; without a v2 close the grade is P&L-only).
    rows: list[CallRecompute] = []
    for c in calls:
        stored = c["outcome"]
        _, canonical = canonical_of[stored]
        close_status = close_status_of[canonical]
        old_close = old_close_of[canonical]
        new_close = new_close_of[canonical]

        v1_grade = store.get_call_grade_version(c["call_id"], 1)
        old_clv = v1_grade["clv_reaction"] if v1_grade else None

        settled_row = settled_by_call.get(c["call_id"])
        if settled_row is None:
            grade_status = "no_settlement"
            new_clv = None
        elif store.get_call_grade_version(c["call_id"], CALL_GRADE_METHOD_VERSION) is not None:
            grade_status = "already_graded"
            v2_grade = store.get_call_grade_version(c["call_id"], CALL_GRADE_METHOD_VERSION)
            new_clv = v2_grade["clv_reaction"] if v2_grade else None
        elif dry_run:
            grade_status = "would_grade"
            new_clv = (
                old_clv + (new_close - old_close)
                if old_clv is not None and old_close is not None and new_close is not None
                else None
            )
        else:
            settlement = Settlement(
                outcome_key=canonical,
                result=settled_row["result"],
                settled_at=_parse_ts(settled_row["settled_at"]) or now,
            )
            _grade_settled_call(
                store,
                c["call_id"],
                canonical,
                settlement,
                now,
                close_version=CLOSING_LINE_METHOD_VERSION,
            )
            grade_status = "graded"
            v2_grade = store.get_call_grade_version(c["call_id"], CALL_GRADE_METHOD_VERSION)
            new_clv = v2_grade["clv_reaction"] if v2_grade else None

        rows.append(
            CallRecompute(
                call_id=c["call_id"],
                market_id=c["market_id"],
                side=c["side"],
                stored_key=stored,
                canonical_key=canonical,
                remapped=canonical != stored,
                old_close=old_close,
                new_close=new_close,
                close_status=close_status,
                old_clv=old_clv,
                new_clv=new_clv,
                settled=settled_row is not None,
                grade_status=grade_status,
            )
        )

    sanity = _sanity_check_vig_removal(rows)
    return RecomputeReport(dry_run=dry_run, calls=rows, remaps=remaps, sanity=sanity)


def _sanity_check_vig_removal(rows: list[CallRecompute]) -> SanityResult:
    """On -110-style markets the avg CLV should drop ~2.4 points.

    v1 closes were vig-included (~0.524 on -110/-110); v2 closes are
    devigged (~0.50). Fills don't change, so CLV moves one-for-one with
    the close. If the drop isn't there, the vig still isn't being removed
    somewhere.
    """
    lo, hi = VIG_CHECK_BAND
    drops = []
    olds = []
    news = []
    for r in rows:
        if (
            r.old_close is None
            or r.new_close is None
            or r.old_clv is None
            or not (lo <= r.old_close <= hi)
        ):
            continue
        new_clv = r.old_clv + (r.new_close - r.old_close)
        olds.append(r.old_clv)
        news.append(new_clv)
        drops.append(r.old_clv - new_clv)
    n = len(drops)
    if n < VIG_CHECK_MIN_N:
        return SanityResult("insufficient_data", n, None, None, None)
    avg_drop = mean(drops)
    status = "pass" if abs(avg_drop - VIG_DROP_EXPECTED) <= VIG_DROP_TOLERANCE else "fail"
    return SanityResult(status, n, mean(olds), mean(news), avg_drop)


def _fmt_prob(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


def format_report(report: RecomputeReport, db: str) -> str:
    """Render the migration report for the terminal."""
    lines = []
    mode = "DRY RUN (no writes)" if report.dry_run else "APPLY (writes committed)"
    lines.append(f"Closing-line/grade recompute [{mode}]")
    lines.append(f"DB: {db}")
    lines.append("")

    lines.append(f"KEY REMAPS ({len(report.remaps)}):")
    for old, new in report.remaps:
        lines.append(f"  {old}")
        lines.append(f"    -> {new}")
    if not report.remaps:
        lines.append("  (none: all stored keys already canonical)")
    lines.append("")

    close_counts: dict[str, int] = {}
    for r in report.calls:
        close_counts[r.close_status] = close_counts.get(r.close_status, 0) + 1
    grade_counts: dict[str, int] = {}
    for r in report.calls:
        grade_counts[r.grade_status] = grade_counts.get(r.grade_status, 0) + 1
    lines.append(f"CLOSES: {close_counts}")
    lines.append(f"GRADES: {grade_counts}")
    lines.append("")

    lines.append("PER CALL:")
    lines.append(
        "  call_id | side | old_close | new_close | old_clv | new_clv | close_status | grade_status"
    )
    for r in report.calls:
        flag = " [remapped]" if r.remapped else ""
        lines.append(
            f"  {r.call_id} | {r.side} | {_fmt_prob(r.old_close)} |"
            f" {_fmt_prob(r.new_close)} | {_fmt_prob(r.old_clv)} |"
            f" {_fmt_prob(r.new_clv)} | {r.close_status} |"
            f" {r.grade_status}{flag}"
        )
    lines.append("")

    # Calls that still can't be CLV-graded, by reason: unsettled calls get
    # no grade at all; settled calls whose close failed get a P&L-only
    # grade. (In dry-run, a "would_grade" call with a "would_recompute"
    # close is not blocked: the CLV just isn't displayed.)
    ungradable: dict[str, int] = {}
    for r in report.calls:
        if r.new_clv is not None:
            continue
        if not r.settled:
            reason = "no_settlement"
        elif r.close_status.startswith("failed:"):
            reason = r.close_status[len("failed:") :]
        else:
            continue
        ungradable[reason] = ungradable.get(reason, 0) + 1
    lines.append(f"CALLS THAT STILL CAN'T BE CLV-GRADED ({sum(ungradable.values())}): {ungradable}")
    lines.append("")

    s = report.sanity
    if s is not None:
        lines.append(f"SANITY CHECK (vig removal on -110-style markets): {s.status.upper()}")
        if s.status == "insufficient_data":
            lines.append(f"  n={s.n} (need {VIG_CHECK_MIN_N}); cannot verify the vig drop.")
        else:
            lines.append(
                f"  n={s.n}, avg old CLV={s.avg_old_clv:.4f},"
                f" avg new CLV={s.avg_new_clv:.4f}, drop={s.drop:.4f}"
                f" (expected ~{VIG_DROP_EXPECTED:.3f})"
            )
            if s.status == "fail":
                lines.append(
                    "  WARNING: the vig drop is not there; the vig still is not"
                    " being removed somewhere. Do not trust these closes."
                )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recompute closing lines and grades (append-only)."
    )
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--config", default=None, help="config file (model settings)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write v2 rows; without it this is a dry run",
    )
    args = parser.parse_args()

    from mindgod.adapters.config import load_settings
    from mindgod.adapters.store import Store
    from mindgod.application.pricing import WeightedConsensusModel

    settings = load_settings(args.config)
    pricing = settings.pricing
    model = WeightedConsensusModel(
        method=pricing.devig_method,
        book_weights=pricing.book_weights,
        min_standard_error=pricing.min_standard_error,
        max_quote_age_s=pricing.max_quote_age_s,
        stale_se_per_minute=pricing.stale_se_per_minute,
    )
    store = Store(args.db)
    report = recompute_closes_and_grades(store, model, dry_run=not args.apply)
    print(format_report(report, args.db))


if __name__ == "__main__":
    main()
