"""Recompute migration: re-keying, append-only versioning, idempotency.

Covers Manish's four requirements:

1. Append-only: v1 closing lines and grades are never overwritten or
   deleted; v2 rows are appended and readers take the latest version.
   has_closing_line() is version-aware (a v1 row does not count).
2. Re-key old MLB rows: a No-side call stored under "margin <= 0"
   (pre-af2b1b7) is re-normalized to "margin <= -1" via the remap table,
   so it joins to its recomputed close instead of silently staying
   ungraded.
3. Idempotent + dry-run: the dry run writes no data rows; applying twice
   is a no-op the second time.
4. Sanity check: on -110-style markets the average CLV drops ~2.4 points
   after the recompute (the vig-removal proof).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mindgod.adapters.store import Store
from mindgod.application.calls import (
    CALL_GRADE_METHOD_VERSION,
    CLOSING_LINE_METHOD_VERSION,
    CallGrade,
    ClosingLine,
    PaperFill,
    Settlement,
)
from mindgod.application.ports import ListingKey, PricedOutcome
from mindgod.application.pricing import (
    WeightedConsensusModel,
    outcome_key,
    parse_outcome_key,
)
from mindgod.application.recompute import (
    CallRecompute,
    _sanity_check_vig_removal,
    format_report,
    recompute_closes_and_grades,
)
from mindgod.domain.primitives import Observation, Probability
from mindgod.domain.propositions import moneyline, spread
from mindgod.domain.quotes import SportsbookQuote
from mindgod.domain.sports import Event, EventId, League, TeamId
from mindgod.domain.terms import Payoff, Terms
from mindgod.domain.venues import VenueId

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
KALSHI = VenueId("kalshi")
DK = VenueId("draftkings")
FD = VenueId("fanduel")
NYY = TeamId("mlb-nyy")
BOS = TeamId("mlb-bos")
KC = TeamId("nfl-kc")
BUF = TeamId("nfl-buf")


def _mlb_event():
    return Event(
        id=EventId("mlb-nyy-bos-20260926"),
        league=League.MLB,
        home=NYY,
        away=BOS,
        scheduled_start=datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC),
    )


def _nfl_event():
    return Event(
        id=EventId("nfl-kc-buf-20261005"),
        league=League.NFL,
        home=KC,
        away=BUF,
        scheduled_start=datetime(2026, 10, 5, 17, 0, 0, tzinfo=UTC),
    )


def _terms(outcome):
    return Terms(payoff=Payoff(outcome, refunds_if=None), void_policy="refund")


def _record_moneyline_quotes(store, event, yes_outcome, no_outcome, yes_odds, no_odds):
    """Book quotes for a Yes/No pair, confirmed 15 min before start."""
    valid_at = event.scheduled_start - timedelta(hours=2)
    recorded_at = event.scheduled_start - timedelta(minutes=15)
    priced = []
    for venue in [DK, FD]:
        for outcome, odds in [(yes_outcome, yes_odds), (no_outcome, no_odds)]:
            key = ListingKey(venue_id=venue, market_id="test", side="yes")
            obs = Observation(valid_at=valid_at, recorded_at=recorded_at)
            quote = SportsbookQuote(listing=key, american_odds=odds, observed=obs)
            priced.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=key,
                    quote=quote,
                    market_group=f"{venue}:test",
                    terms=_terms(outcome),
                )
            )
    store.record_priced(priced, recorded_at)


def _insert_legacy_call(store, call_id, outcome_str, event_start, side, market_id):
    """Simulate a row written by the pre-af2b1b7 code: raw outcome string."""
    store._conn.execute(
        "INSERT INTO calls (call_id, created_at, venue, market_id, side,"
        " outcome, fair_prob, fair_se, fair_method, limit_price_x,"
        " contracts_n, ask_at_alert, net_edge_at_alert, depth_at_x,"
        " event_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            (event_start - timedelta(hours=3)).isoformat(),
            "kalshi",
            market_id,
            side,
            outcome_str,
            0.45,
            0.02,
            "power-devig",
            0.55,
            10,
            0.56,
            0.01,
            100,
            event_start.isoformat(),
        ),
    )
    store._conn.commit()


def _record_reaction_fill(store, call_id, price=Decimal("0.55")):
    store.record_paper_fill(
        PaperFill(
            call_id=call_id,
            kind="reaction",
            contracts=10,
            avg_price=price,
            fee=Decimal("0.10"),
            filled=True,
            at=NOW - timedelta(hours=4),
        )
    )


def _model():
    return WeightedConsensusModel(
        method="power",
        book_weights={str(DK): 2.0, str(FD): 1.0},
        max_quote_age_s=3600,
    )


def _data_row_counts(store):
    cl = store._conn.execute("SELECT COUNT(*) FROM closing_lines").fetchone()[0]
    gr = store._conn.execute("SELECT COUNT(*) FROM call_grades").fetchone()[0]
    rm = store._conn.execute("SELECT COUNT(*) FROM outcome_key_remap").fetchone()[0]
    return cl, gr, rm


# --- key parsing / re-normalization ----------------------------------------


def test_parse_outcome_key_roundtrip():
    event = _nfl_event()
    o = spread(event, KC, Decimal("-3.5"))
    key = outcome_key(o)
    parsed = parse_outcome_key(key)
    assert parsed is not None
    assert outcome_key(parsed) == key


def test_parse_outcome_key_renormalizes_old_mlb_key():
    """A pre-af2b1b7 MLB No key ('margin <= 0') re-normalizes to '<= -1'."""
    event = _mlb_event()
    canonical_no = moneyline(event, NYY).complement()
    assert canonical_no is not None
    old_key = f"mlb|{event.id}|margin|full_game|||threshold|<=|0"
    parsed = parse_outcome_key(old_key)
    assert parsed is not None
    assert outcome_key(parsed) == outcome_key(canonical_no)
    assert outcome_key(parsed) != old_key


def test_parse_outcome_key_leaves_nfl_alone():
    event = _nfl_event()
    key = f"nfl|{event.id}|margin|full_game|||threshold|<=|0"
    parsed = parse_outcome_key(key)
    assert parsed is not None
    assert outcome_key(parsed) == key


def test_parse_outcome_key_rejects_garbage():
    assert parse_outcome_key("not-a-key") is None
    assert parse_outcome_key("mlb|x|margin|full_game|||threshold|<=|0|extra") is None
    assert parse_outcome_key("xxx|x|margin|full_game|||threshold|<=|0") is None


# --- version-aware accessors ------------------------------------------------


def test_version_aware_has_closing_line():
    store = Store(":memory:")
    key = "nfl|e|margin|full_game|||threshold|>=|4"
    # A v1 row alone does not count: the outcome still needs capturing.
    store.record_closing_line(
        ClosingLine(
            outcome_key=key,
            sharp_close_prob=0.5238,
            source="test",
            captured_at=NOW,
            method_version=1,
        )
    )
    assert store.has_closing_line(key) is False
    assert store.get_closing_line(key)["method_version"] == 1

    store.record_closing_line(
        ClosingLine(
            outcome_key=key,
            sharp_close_prob=0.5,
            source="test",
            captured_at=NOW,
            method_version=CLOSING_LINE_METHOD_VERSION,
        )
    )
    assert store.has_closing_line(key) is True
    # Readers take the latest version present.
    assert store.get_closing_line(key)["sharp_close_prob"] == pytest.approx(0.5)
    assert store.get_closing_line_version(key, 1)["sharp_close_prob"] == pytest.approx(0.5238)


def test_version_aware_get_call_grade():
    store = Store(":memory:")
    store.record_call_grade(
        CallGrade(
            call_id="c1",
            clv_reaction=0.02,
            clv_manual=None,
            pnl_reaction=0.44,
            pnl_manual=None,
            edge_half_life_s=None,
            graded_at=NOW,
            method_version=1,
        )
    )
    assert store.get_call_grade("c1")["clv_reaction"] == pytest.approx(0.02)
    store.record_call_grade(
        CallGrade(
            call_id="c1",
            clv_reaction=-0.004,
            clv_manual=None,
            pnl_reaction=0.44,
            pnl_manual=None,
            edge_half_life_s=None,
            graded_at=NOW,
            method_version=CALL_GRADE_METHOD_VERSION,
        )
    )
    assert store.get_call_grade("c1")["clv_reaction"] == pytest.approx(-0.004)
    assert store.get_call_grade_version("c1", 1)["clv_reaction"] == pytest.approx(0.02)


# --- end-to-end migration ----------------------------------------------------


def _seed_migration_db():
    """DB with a legacy MLB call (old '<= 0' key) and a legacy NFL call
    (v1 close + v1 grade under its still-canonical key)."""
    store = Store(":memory:")
    mlb = _mlb_event()
    nfl = _nfl_event()

    nyy_yes = moneyline(mlb, NYY)  # margin >= 1
    bos_yes = moneyline(mlb, BOS)  # margin <= -1 (canonical No-on-NYY)
    old_mlb_no_key = f"mlb|{mlb.id}|margin|full_game|||threshold|<=|0"
    _record_moneyline_quotes(store, mlb, nyy_yes, bos_yes, -150, 130)

    nfl_yes = spread(nfl, KC, Decimal("-3.5"))
    nfl_no = nfl_yes.complement()
    assert nfl_no is not None
    _record_moneyline_quotes(store, nfl, nfl_yes, nfl_no, -110, -110)

    # Legacy MLB call: No on NYY stored under the old "<= 0" key.
    _insert_legacy_call(store, "call-mlb", old_mlb_no_key, mlb.scheduled_start, "no", "KXMLB-1")
    _record_reaction_fill(store, "call-mlb")
    store.record_settlement(Settlement(outcome_key=old_mlb_no_key, result="win", settled_at=NOW))

    # Legacy NFL call: key already canonical; v1 close (vig-included) and
    # v1 grade exist and must be preserved, not overwritten.
    nfl_key = outcome_key(nfl_yes)
    _insert_legacy_call(store, "call-nfl", nfl_key, nfl.scheduled_start, "yes", "KXNFL-1")
    _record_reaction_fill(store, "call-nfl")
    store.record_settlement(Settlement(outcome_key=nfl_key, result="win", settled_at=NOW))
    v1_close = Probability.from_american_odds(-110).value  # 0.5238, vig included
    store.record_closing_line(
        ClosingLine(
            outcome_key=nfl_key,
            sharp_close_prob=v1_close,
            source="legacy",
            captured_at=NOW,
            method_version=1,
        )
    )
    store.record_call_grade(
        CallGrade(
            call_id="call-nfl",
            clv_reaction=v1_close - 0.55 - 0.01,
            clv_manual=None,
            pnl_reaction=(1.0 - 0.55) - 0.01,
            pnl_manual=None,
            edge_half_life_s=None,
            graded_at=NOW,
            method_version=1,
        )
    )
    return store, mlb, nfl, old_mlb_no_key, outcome_key(bos_yes), nfl_key, v1_close


def test_recompute_dry_run_writes_nothing():
    store, mlb, nfl, old_key, canonical_mlb, nfl_key, v1_close = _seed_migration_db()
    before = _data_row_counts(store)
    report = recompute_closes_and_grades(store, _model(), dry_run=True, now=NOW)
    assert _data_row_counts(store) == before

    assert report.dry_run is True
    # The MLB key moved; the NFL key was already canonical.
    assert (old_key, canonical_mlb) in report.remaps
    assert len(report.remaps) == 1

    by_id = {r.call_id: r for r in report.calls}
    mlb_row = by_id["call-mlb"]
    assert mlb_row.remapped is True
    assert mlb_row.close_status == "would_recompute"
    assert mlb_row.new_close == pytest.approx(0.416017, abs=1e-4)
    assert mlb_row.old_close is None  # old MLB closes never paired
    assert mlb_row.grade_status == "would_grade"

    nfl_row = by_id["call-nfl"]
    assert nfl_row.remapped is False
    assert nfl_row.close_status == "would_recompute"
    assert nfl_row.old_close == pytest.approx(v1_close, abs=1e-9)
    assert nfl_row.new_close == pytest.approx(0.5, abs=1e-6)

    text = format_report(report, ":memory:")
    assert "DRY RUN" in text
    assert "would_recompute" in text


def test_recompute_apply_rekeys_and_appends():
    store, mlb, nfl, old_key, canonical_mlb, nfl_key, v1_close = _seed_migration_db()
    report = recompute_closes_and_grades(store, _model(), dry_run=False, now=NOW)
    assert report.dry_run is False

    # Remap recorded: old "<= 0" -> canonical "<= -1".
    assert store.canonical_outcome_key(old_key) == canonical_mlb

    # Append-only: the v1 close and v1 grade rows still exist untouched.
    v1 = store.get_closing_line_version(nfl_key, 1)
    assert v1 is not None and v1["sharp_close_prob"] == pytest.approx(v1_close)
    assert store.get_call_grade_version("call-nfl", 1) is not None

    # v2 close under the canonical MLB key: devigged BOS side (+130).
    mlb_close = store.get_closing_line(canonical_mlb)
    assert mlb_close is not None
    assert mlb_close["method_version"] == CLOSING_LINE_METHOD_VERSION
    assert mlb_close["sharp_close_prob"] == pytest.approx(0.416017, abs=1e-4)
    # The old key itself has no close: nothing was recorded under it.
    assert store.get_closing_line(old_key) is None

    # v2 close for the NFL pair: devigged -110/-110 is 0.50.
    nfl_close = store.get_closing_line(nfl_key)
    assert nfl_close["sharp_close_prob"] == pytest.approx(0.5, abs=1e-6)
    assert nfl_close["method_version"] == CLOSING_LINE_METHOD_VERSION

    # v2 grades through the production path: CLV = close - fill - fee.
    mlb_grade = store.get_call_grade("call-mlb")
    assert mlb_grade["method_version"] == CALL_GRADE_METHOD_VERSION
    assert mlb_grade["clv_reaction"] == pytest.approx(0.416017 - 0.55 - 0.01, abs=1e-4)
    assert mlb_grade["pnl_reaction"] == pytest.approx((1.0 - 0.55) - 0.01, abs=1e-9)

    nfl_grade = store.get_call_grade("call-nfl")
    assert nfl_grade["method_version"] == CALL_GRADE_METHOD_VERSION
    assert nfl_grade["clv_reaction"] == pytest.approx(0.5 - 0.55 - 0.01, abs=1e-6)

    # Capture loop sees the canonical outcome as captured (version-aware).
    assert store.has_closing_line(canonical_mlb) is True

    by_id = {r.call_id: r for r in report.calls}
    assert by_id["call-mlb"].grade_status == "graded"
    assert by_id["call-nfl"].grade_status == "graded"


def test_recompute_is_idempotent():
    store, *_ = _seed_migration_db()
    recompute_closes_and_grades(store, _model(), dry_run=False, now=NOW)
    after_first = _data_row_counts(store)
    report = recompute_closes_and_grades(store, _model(), dry_run=False, now=NOW)
    assert _data_row_counts(store) == after_first
    by_id = {r.call_id: r for r in report.calls}
    assert by_id["call-mlb"].close_status == "already_current"
    assert by_id["call-mlb"].grade_status == "already_graded"
    assert by_id["call-nfl"].close_status == "already_current"
    assert by_id["call-nfl"].grade_status == "already_graded"


def test_recompute_ungradable_breakdown():
    store = Store(":memory:")
    mlb = _mlb_event()
    nyy_yes = moneyline(mlb, NYY)
    bos_yes = moneyline(mlb, BOS)
    assert bos_yes is not None

    # Stale quotes: confirmed 5h before start, max age 1h -> "stale".
    valid_at = mlb.scheduled_start - timedelta(hours=6)
    recorded_at = mlb.scheduled_start - timedelta(hours=5)
    priced = []
    for venue in [DK, FD]:
        for outcome, odds in [(nyy_yes, -150), (bos_yes, 130)]:
            key = ListingKey(venue_id=venue, market_id="test", side="yes")
            obs = Observation(valid_at=valid_at, recorded_at=recorded_at)
            quote = SportsbookQuote(listing=key, american_odds=odds, observed=obs)
            priced.append(
                PricedOutcome(
                    outcome=outcome,
                    listing_key=key,
                    quote=quote,
                    market_group=f"{venue}:test",
                    terms=_terms(outcome),
                )
            )
    store.record_priced(priced, recorded_at)

    old_key = f"mlb|{mlb.id}|margin|full_game|||threshold|<=|0"
    _insert_legacy_call(store, "call-stale", old_key, mlb.scheduled_start, "no", "KX-1")
    _record_reaction_fill(store, "call-stale")
    store.record_settlement(Settlement(outcome_key=old_key, result="loss", settled_at=NOW))
    # Never settled: no grade possible. (A different outcome key so the
    # settlement row above does not join to it.)
    _insert_legacy_call(
        store,
        "call-open",
        "nfl|nfl-test-20261005|margin|full_game|||threshold|>=|4",
        mlb.scheduled_start,
        "yes",
        "KX-2",
    )

    report = recompute_closes_and_grades(store, _model(), dry_run=True, now=NOW)
    by_id = {r.call_id: r for r in report.calls}
    assert by_id["call-stale"].close_status == "failed:stale"
    assert by_id["call-stale"].grade_status == "would_grade"  # P&L-only grade
    assert by_id["call-stale"].new_clv is None
    assert by_id["call-open"].grade_status == "no_settlement"

    text = format_report(report, ":memory:")
    assert "stale" in text and "no_settlement" in text


# --- sanity check ------------------------------------------------------------


def _sanity_row(old_close, new_close, old_clv):
    return CallRecompute(
        call_id="c",
        market_id="m",
        side="yes",
        stored_key="k",
        canonical_key="k",
        remapped=False,
        old_close=old_close,
        new_close=new_close,
        close_status="recomputed",
        old_clv=old_clv,
        new_clv=None,
        settled=True,
        grade_status="graded",
    )


def test_sanity_check_passes_on_vig_removal():
    rows = [_sanity_row(0.5238, 0.5, 0.02 + (i * 0.001)) for i in range(6)]
    result = _sanity_check_vig_removal(rows)
    assert result.status == "pass"
    assert result.drop == pytest.approx(0.0238, abs=1e-4)


def test_sanity_check_fails_when_vig_remains():
    rows = [_sanity_row(0.5238, 0.5238, 0.02) for _ in range(6)]
    result = _sanity_check_vig_removal(rows)
    assert result.status == "fail"
    assert result.drop == pytest.approx(0.0, abs=1e-9)


def test_sanity_check_insufficient_data():
    rows = [_sanity_row(0.5238, 0.5, 0.02) for _ in range(3)]
    assert _sanity_check_vig_removal(rows).status == "insufficient_data"
    # Non -110-style markets don't count either.
    rows = [_sanity_row(0.70, 0.68, 0.02) for _ in range(6)]
    assert _sanity_check_vig_removal(rows).status == "insufficient_data"


def test_sanity_check_in_migration_report():
    store, *_ = _seed_migration_db()
    report = recompute_closes_and_grades(store, _model(), dry_run=True, now=NOW)
    # Only one -110-style call in the seed DB: not enough for the check.
    assert report.sanity is not None
    assert report.sanity.status == "insufficient_data"
