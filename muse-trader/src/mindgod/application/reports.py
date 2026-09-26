"""ADR-0008 reports: slice call grades by detector, league, market type, etc.

The evaluation loop: shows where the edge is real and where the model is
fooling itself. CLV is the headline metric; P&L is reported but never drives
decisions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from statistics import mean


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
    """Slice grades by limit price bucket (0-20c, 20-40c, ...)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT c.limit_price_x, g.clv_reaction, g.clv_manual,
                  g.pnl_reaction, g.edge_half_life_s, p.filled
           FROM call_grades g
           JOIN calls c ON c.call_id = g.call_id
           LEFT JOIN paper_fills p ON p.call_id = g.call_id AND p.kind = 'reaction'
           WHERE g.method_version = (SELECT MAX(g2.method_version)
                                     FROM call_grades g2
                                     WHERE g2.call_id = g.call_id)
        """
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
    """Overall summary across all graded calls."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT g.clv_reaction, g.clv_manual, g.pnl_reaction,
                  g.edge_half_life_s, p.filled
           FROM call_grades g
           LEFT JOIN paper_fills p ON p.call_id = g.call_id AND p.kind = 'reaction'
           WHERE g.method_version = (SELECT MAX(g2.method_version)
                                     FROM call_grades g2
                                     WHERE g2.call_id = g.call_id)
        """
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


def format_report(slices: list[GradeSlice]) -> str:
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
    return "\n".join(lines)
