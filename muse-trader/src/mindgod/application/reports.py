"""ADR-0008 reports: slice call grades by detector, league, market type, etc.

The evaluation loop: shows where the edge is real and where the model is
fooling itself. CLV is the headline metric; P&L is reported but never drives
decisions.

Reports read only the current grade method version. A call the recompute
could not regrade (no settlement, no complement pair, stale quotes) keeps
its v1 row in the table but is excluded here: letting MAX(method_version)
stand in would mix vig-biased v1 grades back into the averages, the exact
bias the migration exists to remove. Use excluded_clv_breakdown() to see
who was left out and why.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from statistics import mean

from .calls import CALL_GRADE_METHOD_VERSION
from .ports import FairValueModel


@dataclass(frozen=True, slots=True)
class GradeSlice:
    """Aggregated grades for one slice (e.g. one league, one price bucket)."""

    label: str
    n_calls: int
    n_filled_reaction: int
    avg_clv_reaction: float | None
    avg_clv_manual: float | None
    avg_pnl_reaction: float | None
    avg_half_life_s: float | None


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return mean(vals) if vals else None


def report_by_price_bucket(db_path: str) -> list[GradeSlice]:
    """Slice grades by limit price bucket (0-20c, 20-40c, ...).

    Current method version only; see the module docstring.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT c.limit_price_x, g.clv_reaction, g.clv_manual,
                  g.pnl_reaction, g.edge_half_life_s, p.filled
           FROM call_grades g
           JOIN calls c ON c.call_id = g.call_id
           LEFT JOIN paper_fills p ON p.call_id = g.call_id AND p.kind = 'reaction'
           WHERE g.method_version = ?
        """,
        (CALL_GRADE_METHOD_VERSION,),
    ).fetchall()
    conn.close()

    buckets: dict[str, list[tuple[float | None, ...]]] = {}
    for limit_x, clv_r, clv_m, pnl_r, half_life, filled in rows:
        bucket = f"{int(limit_x * 100) // 20 * 20}-{(int(limit_x * 100) // 20 + 1) * 20}c"
        buckets.setdefault(bucket, []).append((clv_r, clv_m, pnl_r, half_life, filled))

    result = []
    for label in sorted(buckets):
        vals = buckets[label]
        result.append(
            GradeSlice(
                label=label,
                n_calls=len(vals),
                n_filled_reaction=sum(1 for v in vals if v[4]),
                avg_clv_reaction=_avg([v[0] for v in vals]),
                avg_clv_manual=_avg([v[1] for v in vals]),
                avg_pnl_reaction=_avg([v[2] for v in vals]),
                avg_half_life_s=_avg([v[3] for v in vals]),
            )
        )
    return result


def report_summary(db_path: str) -> GradeSlice:
    """Overall summary across all graded calls.

    Current method version only; see the module docstring.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT g.clv_reaction, g.clv_manual, g.pnl_reaction,
                  g.edge_half_life_s, p.filled
           FROM call_grades g
           LEFT JOIN paper_fills p ON p.call_id = g.call_id AND p.kind = 'reaction'
           WHERE g.method_version = ?
        """,
        (CALL_GRADE_METHOD_VERSION,),
    ).fetchall()
    conn.close()

    return GradeSlice(
        label="all",
        n_calls=len(rows),
        n_filled_reaction=sum(1 for r in rows if r[4]),
        avg_clv_reaction=_avg([r[0] for r in rows]),
        avg_clv_manual=_avg([r[1] for r in rows]),
        avg_pnl_reaction=_avg([r[2] for r in rows]),
        avg_half_life_s=_avg([r[3] for r in rows]),
    )


def excluded_clv_breakdown(db_path: str, model: FairValueModel) -> dict[str, int]:
    """Calls with no CLV at the current method version, by reason.

    The same breakdown the recompute dry run prints: `no_settlement`,
    or the close failure reason (`no_complement`, `no_quotes`, `no_pair`,
    `stale`, `devig_failed`, `no_consensus`). Covers both calls excluded
    from the report entirely (no current-version grade row) and calls
    present P&L-only (current-version row with NULL clv_reaction).

    The reasons come from the recompute's own dry-run classification, so
    they cannot drift from what the migration reports. `model` is the
    same fair-value model the recompute runs with (built from settings);
    the close-failure reasons depend on its config.
    """
    from mindgod.adapters.store import Store

    from .recompute import recompute_closes_and_grades

    store = Store(db_path)
    report = recompute_closes_and_grades(store, model, dry_run=True)
    out: dict[str, int] = {}
    for r in report.calls:
        v2 = store.get_call_grade_version(r.call_id, CALL_GRADE_METHOD_VERSION)
        if v2 is not None and v2["clv_reaction"] is not None:
            continue  # has a current-version CLV: included in the averages
        if not r.settled:
            reason = "no_settlement"
        elif r.close_status.startswith("failed:"):
            reason = r.close_status[len("failed:") :]
        else:
            # Settled and the close would compute: the recompute has not
            # been applied (or re-applied) for this call yet.
            reason = "awaiting_recompute"
        out[reason] = out.get(reason, 0) + 1
    return out


def format_report(slices: list[GradeSlice], exclusions: dict[str, int] | None = None) -> str:
    """Human-readable report."""
    lines = ["Call grades by price bucket:", ""]
    lines.append(
        f"{'Bucket':<10} {'Calls':>6} {'Filled':>6} {'CLV_r':>8} {'CLV_m':>8} "
        f"{'PnL_r':>8} {'HalfLife':>8}"
    )
    for s in slices:
        clv_r = f"{s.avg_clv_reaction:+.3f}" if s.avg_clv_reaction is not None else "n/a"
        clv_m = f"{s.avg_clv_manual:+.3f}" if s.avg_clv_manual is not None else "n/a"
        pnl_r = f"{s.avg_pnl_reaction:+.3f}" if s.avg_pnl_reaction is not None else "n/a"
        hl = f"{s.avg_half_life_s:.0f}s" if s.avg_half_life_s is not None else "n/a"
        lines.append(
            f"{s.label:<10} {s.n_calls:>6} {s.n_filled_reaction:>6} "
            f"{clv_r:>8} {clv_m:>8} {pnl_r:>8} {hl:>8}"
        )
    if exclusions is not None:
        lines.append("")
        lines.append(
            f"CALLS WITH NO CLV AT v{CALL_GRADE_METHOD_VERSION} "
            f"({sum(exclusions.values())}): {exclusions}"
        )
    return "\n".join(lines)
