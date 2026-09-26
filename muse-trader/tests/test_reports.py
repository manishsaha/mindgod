"""Reports read only the current grade version; exclusions are reported.

Manish's bug: the old reports took MAX(method_version) per call, so a call
the recompute could not regrade kept its vig-biased v1 grade as "latest"
and leaked back into the averages. Now reports filter to the current
version, excluded calls are broken down by reason, and (call_id,
method_version) is unique so a duplicate grade row can never double-count.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_recompute import (
    _insert_legacy_call,
    _model,
    _nfl_event,
    _record_reaction_fill,
)

from mindgod.adapters.store import Store
from mindgod.application.calls import CALL_GRADE_METHOD_VERSION, CallGrade, Settlement
from mindgod.application.ports import ListingKey, PricedOutcome
from mindgod.application.pricing import outcome_key
from mindgod.application.recompute import recompute_closes_and_grades
from mindgod.application.reports import (
    excluded_clv_breakdown,
    format_report,
    report_by_price_bucket,
    report_summary,
)
from mindgod.domain.primitives import Observation
from mindgod.domain.propositions import spread
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import VenueId

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
KC = TeamId("nfl-kc")


def _grade_v1(store, call_id, clv):
    store.record_call_grade(
        CallGrade(
            call_id=call_id,
            clv_reaction=clv,
            clv_manual=None,
            pnl_reaction=0.44,
            pnl_manual=None,
            edge_half_life_s=None,
            graded_at=NOW,
            method_version=1,
        )
    )


def _seed_report_db(path):
    """Three v1-graded calls: one cleanly regradable, one whose close is
    stale (P&L-only v2), one never settled (no v2 at all)."""
    store = Store(path)
    event = _nfl_event()
    DK = VenueId("draftkings")
    FD = VenueId("fanduel")

    ok_outcome = spread(event, KC, Decimal("-3.5"))
    stale_outcome = spread(event, KC, Decimal("-6.5"))
    open_outcome = spread(event, KC, Decimal("-10.5"))
    assert ok_outcome.complement() is not None
    assert stale_outcome.complement() is not None

    def terms(o):
        return Terms(payoff=Payoff(o, refunds_if=None), void_policy="refund")

    def record_pair(yes_outcome, no_outcome, recorded_at):
        valid_at = event.scheduled_start - timedelta(hours=2)
        priced = []
        for venue in [DK, FD]:
            for outcome, odds in [(yes_outcome, -110), (no_outcome, -110)]:
                key = ListingKey(venue_id=venue, market_id="test", side="yes")
                obs = Observation(valid_at=valid_at, recorded_at=recorded_at)
                quote = SportsbookQuote(listing=key, american_odds=odds, observed=obs)
                priced.append(
                    PricedOutcome(
                        outcome=outcome,
                        listing_key=key,
                        quote=quote,
                        market_group=f"{venue}:test",
                        terms=terms(outcome),
                    )
                )
        store.record_priced(priced, recorded_at)

    fresh = event.scheduled_start - timedelta(minutes=15)
    record_pair(ok_outcome, ok_outcome.complement(), fresh)
    record_pair(
        stale_outcome,
        stale_outcome.complement(),
        event.scheduled_start - timedelta(hours=5),  # stale: max age 1h
    )

    ok_key = outcome_key(ok_outcome)
    stale_key = outcome_key(stale_outcome)
    open_key = outcome_key(open_outcome)
    _insert_legacy_call(store, "call-ok", ok_key, event.scheduled_start, "yes", "KX-1")
    _insert_legacy_call(store, "call-stale", stale_key, event.scheduled_start, "yes", "KX-2")
    # Distinct outcome key: the ok_key settlement must not join to it.
    _insert_legacy_call(store, "call-open", open_key, event.scheduled_start, "yes", "KX-3")
    for cid in ("call-ok", "call-stale", "call-open"):
        _record_reaction_fill(store, cid)
        _grade_v1(store, cid, 0.02)
    for key in (ok_key, stale_key):
        store.record_settlement(Settlement(outcome_key=key, result="win", settled_at=NOW))
    return store


def test_reports_read_only_current_version(tmp_path):
    path = str(tmp_path / "r.db")
    store = _seed_report_db(path)
    recompute_closes_and_grades(store, _model(), dry_run=False, now=NOW)

    summary = report_summary(path)
    # call-ok (full v2) + call-stale (P&L-only v2). call-open has only a
    # v1 grade: excluded, not leaked in via MAX(version).
    assert summary.n_calls == 2
    # Only call-ok contributes a CLV: devigged 0.5 - 0.55 fill - 0.01 fee.
    assert summary.avg_clv_reaction == pytest.approx(-0.06, abs=1e-9)
    # The v1 grades (clv 0.02) must not move the average.
    assert summary.avg_clv_reaction != pytest.approx(0.02)

    buckets = report_by_price_bucket(path)
    assert sum(s.n_calls for s in buckets) == 2


def test_excluded_clv_breakdown(tmp_path):
    path = str(tmp_path / "r.db")
    store = _seed_report_db(path)
    recompute_closes_and_grades(store, _model(), dry_run=False, now=NOW)

    excluded = excluded_clv_breakdown(path, _model())
    assert excluded == {"no close": 1, "no_settlement": 1}

    text = format_report(report_by_price_bucket(path), excluded)
    assert "NO CLV AT v2" in text
    assert "no close" in text and "no_settlement" in text


def test_excluded_breakdown_before_apply(tmp_path):
    path = str(tmp_path / "r.db")
    _seed_report_db(path)
    # Recompute not applied yet: call-ok would get a full v2 grade,
    # call-stale is settled with no usable close, call-open never settled.
    excluded = excluded_clv_breakdown(path, _model())
    assert excluded == {
        "awaiting_recompute": 1,
        "no close": 1,
        "no_settlement": 1,
    }


def test_call_grade_unique_per_version():
    store = Store(":memory:")

    def grade(version):
        return CallGrade(
            call_id="c1",
            clv_reaction=0.01,
            clv_manual=None,
            pnl_reaction=0.44,
            pnl_manual=None,
            edge_half_life_s=None,
            graded_at=NOW,
            method_version=version,
        )

    store.record_call_grade(grade(CALL_GRADE_METHOD_VERSION))
    with pytest.raises(sqlite3.IntegrityError):
        store.record_call_grade(grade(CALL_GRADE_METHOD_VERSION))
    # A different version is a different row: allowed.
    store.record_call_grade(grade(1))
    rows = store._conn.execute("SELECT COUNT(*) FROM call_grades WHERE call_id = 'c1'").fetchone()[
        0
    ]
    assert rows == 2


def test_unique_index_added_to_legacy_db(tmp_path):
    """A pre-versioning call_grades table gets the column and the unique
    index from the migration path, not just fresh DDL."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE call_grades(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             call_id TEXT NOT NULL,
             clv_reaction REAL, clv_manual REAL, pnl_reaction REAL,
             pnl_manual REAL, edge_half_life_s REAL,
             graded_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO call_grades (call_id, graded_at) VALUES ('c1', '2026-09-26T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = Store(path)  # runs _ensure_method_versioning
    idx = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND tbl_name='call_grades' AND sql LIKE '%UNIQUE%'"
    ).fetchall()
    assert idx, "unique index missing after migration"

    def grade():
        return CallGrade(
            call_id="c1",
            clv_reaction=0.01,
            clv_manual=None,
            pnl_reaction=0.44,
            pnl_manual=None,
            edge_half_life_s=None,
            graded_at=NOW,
            method_version=1,  # legacy row defaulted to v1
        )

    with pytest.raises(sqlite3.IntegrityError):
        store.record_call_grade(grade())


def test_duplicate_grades_fail_startup_with_clear_message(tmp_path):
    """An older DB that already has duplicate grades must fail loudly.

    The unique index is a backstop, not a migration: creating it over
    duplicates would raise a bare IntegrityError and the service would not
    start. The pre-check raises a RuntimeError that says what to do.
    """
    path = str(tmp_path / "dup.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE call_grades(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             call_id TEXT NOT NULL,
             clv_reaction REAL, clv_manual REAL, pnl_reaction REAL,
             pnl_manual REAL, edge_half_life_s REAL,
             graded_at TEXT NOT NULL,
             method_version INTEGER NOT NULL DEFAULT 1)"""
    )
    for ts in ("2026-09-26T00:00:00+00:00", "2026-09-26T01:00:00+00:00"):
        conn.execute(
            "INSERT INTO call_grades (call_id, graded_at, method_version) VALUES ('c1', ?, 2)",
            (ts,),
        )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="call_grades already has duplicate"):
        Store(path)
