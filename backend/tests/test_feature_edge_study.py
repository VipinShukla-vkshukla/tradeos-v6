"""
tools.feature_edge_study — pure analysis functions, no I/O.

Mirrors the split between "pure computation" and "thin I/O boundary" this
project already draws in allocation/hurdle.py and tools/discover_engines.py:
_numeric_split, categorical_splits, _significant, _hour_bucket and
_floor_since take already-fetched data (or nothing) and are fully
offline-testable; _rows/_propose/study/main touch Supabase and are not
covered here — the same reason test_hurdle_dedup.py stops at
_empirical_base and does not test the daemon's own select() call.
"""

from __future__ import annotations

from tests import cfg_ctx


def _row(outcome, outcome_pct=0.0, ts="2026-08-19T04:30:00+00:00",
        meta=None, confidence=0.7, regime="RISK_ON"):
    """04:30 UTC = 10:00 IST — MID by default; override ts to hit OPEN/LATE."""
    return {"outcome": outcome, "outcome_pct": outcome_pct, "ts": ts,
           "meta": meta or {}, "confidence": confidence,
           "regime_at_detection": regime}


# ── _hour_bucket ──────────────────────────────────────────────────────────

def test_hour_bucket_boundaries():
    from tools.feature_edge_study import _hour_bucket
    # 09:15 IST = 03:45 UTC -> OPEN; 09:59 IST still OPEN; 10:00 IST -> MID
    assert _hour_bucket("2026-08-19T03:45:00+00:00") == "OPEN"
    assert _hour_bucket("2026-08-19T04:29:00+00:00") == "OPEN"
    assert _hour_bucket("2026-08-19T04:30:00+00:00") == "MID"
    assert _hour_bucket("2026-08-19T07:29:00+00:00") == "MID"   # 12:59 IST
    assert _hour_bucket("2026-08-19T07:30:00+00:00") == "LATE"  # 13:00 IST
    assert _hour_bucket(None) is None
    assert _hour_bucket("not-a-timestamp") is None


# ── _win_rate / _mean_pct ────────────────────────────────────────────────

def test_win_rate_excludes_timeout_from_the_denominator():
    from tools.feature_edge_study import _win_rate
    rows = [_row("TARGET"), _row("TARGET"), _row("STOP"), _row("TIMEOUT")]
    wr, t, s, o = _win_rate(rows)
    assert t == 2 and s == 1 and o == 1
    assert wr == 2 / 3, "TIMEOUT must not dilute the win rate denominator"


def test_win_rate_is_none_with_no_target_or_stop():
    from tools.feature_edge_study import _win_rate
    wr, t, s, o = _win_rate([_row("TIMEOUT"), _row("TIMEOUT")])
    assert wr is None, "a segment of pure TIMEOUTs says nothing about accuracy"


# ── numeric_split ────────────────────────────────────────────────────────

def test_numeric_split_fires_on_a_real_separation():
    from tools.feature_edge_study import numeric_split
    rows = []
    # bottom third: mostly STOP, low volume_ratio
    for i in range(20):
        rows.append(_row("STOP" if i < 16 else "TARGET", -0.5,
                         meta={"volume_ratio": 0.8 + i * 0.01}))
    # top third: mostly TARGET, high volume_ratio
    for i in range(20):
        rows.append(_row("TARGET" if i < 16 else "STOP", 1.0,
                         meta={"volume_ratio": 3.0 + i * 0.01}))
    found = numeric_split(rows, "volume_ratio", min_segment=10)
    assert found is not None, "a clean 20pp+ win-rate gap must be reported"
    assert found["feature"] == "volume_ratio"
    assert found["hi_win_rate"] > found["lo_win_rate"]


def test_numeric_split_is_silent_when_nothing_separates_the_groups():
    from tools.feature_edge_study import numeric_split
    rows = []
    for i in range(30):
        # win rate ~50% regardless of volume_ratio — no real signal
        rows.append(_row("TARGET" if i % 2 == 0 else "STOP", 0.1,
                         meta={"volume_ratio": 0.5 + i * 0.1}))
    found = numeric_split(rows, "volume_ratio", min_segment=8)
    assert found is None, "a flat relationship must not be reported as a finding"


def test_numeric_split_respects_the_sample_floor():
    """The exact shape that would slip through without a floor: two TINY
    groups with a huge apparent gap (100% vs 0%) — real signal in n=3 is
    still noise, and MIN_SEGMENT exists specifically to refuse it."""
    from tools.feature_edge_study import numeric_split
    rows = [_row("STOP", -1.0, meta={"volume_ratio": 1.0}) for _ in range(3)]
    rows += [_row("TARGET", 1.0, meta={"volume_ratio": 5.0}) for _ in range(3)]
    found = numeric_split(rows, "volume_ratio", min_segment=15)
    assert found is None, "n=3 per side must be refused regardless of the gap size"


def test_numeric_split_ignores_rows_missing_the_feature():
    from tools.feature_edge_study import numeric_split
    rows = [_row("TARGET", 1.0, meta={}) for _ in range(30)]  # no volume_ratio anywhere
    found = numeric_split(rows, "volume_ratio", min_segment=5)
    assert found is None, "a feature absent from every row must not fabricate a split"


def test_numeric_value_tolerates_meta_shipped_as_a_json_string():
    """Same defence _engine_of() already has — meta shipped as a JSON
    string before (F-40) and a reader that assumes a dict silently drops
    every row instead of erroring, which is worse."""
    from tools.feature_edge_study import _numeric_value
    row = {"meta": '{"volume_ratio": 2.5}'}
    assert _numeric_value(row, "volume_ratio") == 2.5


def test_numeric_value_reads_confidence_from_the_column_not_meta():
    from tools.feature_edge_study import _numeric_value
    row = {"confidence": 0.81, "meta": {"confidence": 0.1}}
    assert _numeric_value(row, "confidence") == 0.81, (
        "confidence is a real column on intraday_setups — meta's copy, if "
        "any, must never shadow it")


# ── categorical_splits ───────────────────────────────────────────────────

def test_categorical_split_fires_for_a_standout_bucket():
    from tools.feature_edge_study import categorical_splits
    rows = []
    for i in range(20):
        rows.append(_row("TARGET" if i < 3 else "STOP", -0.3, regime="RISK_OFF"))
    for i in range(20):
        rows.append(_row("TARGET" if i < 14 else "STOP", 0.5, regime="RISK_ON"))
    found = categorical_splits(rows, "regime_at_detection", min_segment=10)
    cats = {f["category"] for f in found}
    assert "RISK_OFF" in cats or "RISK_ON" in cats, (
        f"expected a standout bucket to be reported, got {cats}")


def test_categorical_split_ignores_a_thin_category():
    from tools.feature_edge_study import categorical_splits
    rows = [_row("TARGET", 1.0, regime="RISK_ON") for _ in range(2)]
    rows += [_row("STOP", -0.5, regime="RISK_OFF") for _ in range(40)]
    found = categorical_splits(rows, "regime_at_detection", min_segment=15)
    cats = {f["category"] for f in found}
    assert "RISK_ON" not in cats, "a 2-row category must never clear a 15-row floor"


def test_categorical_split_hour_bucket_reads_ts_not_meta():
    from tools.feature_edge_study import categorical_splits
    rows = []
    for i in range(15):
        rows.append(_row("STOP", -0.4, ts="2026-08-19T03:50:00+00:00"))  # OPEN
    for i in range(15):
        rows.append(_row("TARGET", 0.6, ts="2026-08-19T09:00:00+00:00"))  # LATE (14:30 IST)
    found = categorical_splits(rows, "_hour_bucket", min_segment=10)
    cats = {f["category"] for f in found}
    assert cats, "a clean OPEN-vs-LATE win-rate gap must be reported"


# ── is_favourable — F-50 ─────────────────────────────────────────────────

def test_is_favourable_true_when_hi_win_rate_beats_lo():
    from tools.feature_edge_study import is_favourable
    found = {"hi_win_rate": 0.85, "lo_win_rate": 0.38,
            "hi_mean_pct": None, "lo_mean_pct": None}
    assert is_favourable(found) is True


def test_is_favourable_false_when_hi_win_rate_trails_lo():
    from tools.feature_edge_study import is_favourable
    found = {"hi_win_rate": 0.06, "lo_win_rate": 0.59,
            "hi_mean_pct": None, "lo_mean_pct": None}
    assert is_favourable(found) is False


def test_is_favourable_falls_back_to_mean_pct_when_win_rate_ties():
    """A segment made only of TIMEOUT/UNKNOWN has no win rate at all (see
    _win_rate's own docstring) — mean_pct is the only signal left."""
    from tools.feature_edge_study import is_favourable
    found = {"hi_win_rate": None, "lo_win_rate": None,
            "hi_mean_pct": 0.42, "lo_mean_pct": -0.10}
    assert is_favourable(found) is True


def test_is_favourable_none_when_nothing_can_decide_it():
    from tools.feature_edge_study import is_favourable
    found = {"hi_win_rate": None, "lo_win_rate": None,
            "hi_mean_pct": None, "lo_mean_pct": None}
    assert is_favourable(found) is None


def test_is_favourable_none_on_an_exact_tie():
    """A tie must read as 'cannot decide', never default toward either
    direction — the same discipline _win_rate's None-on-empty already has."""
    from tools.feature_edge_study import is_favourable
    found = {"hi_win_rate": 0.5, "lo_win_rate": 0.5,
            "hi_mean_pct": 0.1, "lo_mean_pct": 0.1}
    assert is_favourable(found) is None


# ── _validation_outcome — F-50 fix, 23-Aug-2026 ─────────────────────────

def test_validation_outcome_confirms_when_directions_agree():
    from tools.feature_edge_study import _validation_outcome
    assert _validation_outcome("favourable", True) is True
    assert _validation_outcome("unfavourable", False) is True


def test_validation_outcome_rejects_when_directions_disagree():
    from tools.feature_edge_study import _validation_outcome
    assert _validation_outcome("favourable", False) is False
    assert _validation_outcome("unfavourable", True) is False


def test_validation_outcome_is_none_for_a_pre_f50_placeholder_value():
    """The exact live bug: a row written before F-50's direction field
    existed carries the old placeholder text. Must be 'cannot validate',
    never silently read as an explicit unfavourable claim."""
    from tools.feature_edge_study import _validation_outcome
    assert _validation_outcome("no feature-level filter", True) is None
    assert _validation_outcome("no feature-level filter", False) is None


def test_validation_outcome_is_none_for_unclear():
    """'unclear' is a genuine original no-opinion (is_favourable returned
    None when the finding was made) — not a value with a real direction
    to check consistency against."""
    from tools.feature_edge_study import _validation_outcome
    assert _validation_outcome("unclear", True) is None
    assert _validation_outcome("unclear", False) is None


def test_validation_outcome_is_none_when_the_fresh_check_cannot_decide():
    from tools.feature_edge_study import _validation_outcome
    assert _validation_outcome("favourable", None) is None


def test_validation_outcome_regression_orb_mid_would_now_validate_correctly():
    """The literal live case: ORB/_hour_bucket/MID's original finding
    predates F-50 (placeholder current_value), and its fresh check agreed
    MID was the good side. The old inline comparison emitted REJECTED;
    this must emit 'cannot validate' (None -> skipped), not a verdict at
    all, since the original claim was never actually recorded."""
    from tools.feature_edge_study import _validation_outcome
    assert _validation_outcome("no feature-level filter", True) is None


# ── target_key_for ───────────────────────────────────────────────────────

def test_target_key_includes_category_so_categories_do_not_collide():
    """The exact bug this function's own docstring describes: two DIFFERENT
    categorical findings for the same (engine, feature) must not produce the
    same brain_proposals dedup key, or the second overwrites the first."""
    from tools.feature_edge_study import target_key_for
    finding_it = {"feature": "sector", "category": "i.t"}
    finding_metals = {"feature": "sector", "category": "metals & mining"}
    key_it = target_key_for("SDN", finding_it)
    key_metals = target_key_for("SDN", finding_metals)
    assert key_it != key_metals, (
        f"two different sector findings collided on one key: {key_it!r}")


def test_target_key_for_a_numeric_split_has_no_category_suffix():
    from tools.feature_edge_study import target_key_for
    finding = {"feature": "volume_ratio"}  # numeric splits carry no "category"
    assert target_key_for("ORB", finding) == "ORB/volume_ratio"


# ── _floor_since ─────────────────────────────────────────────────────────

def test_floor_since_explicit_override_wins_outright():
    from tools.feature_edge_study import _floor_since
    assert _floor_since("2020-01-01") == "2020-01-01"


def test_floor_since_honours_priors_intraday_since_like_the_allocator_does():
    from tools.feature_edge_study import _floor_since
    with cfg_ctx({"priors_intraday_lookback_days": "90",
                 "priors_intraday_since": "2026-08-20"}):
        assert _floor_since(None) == "2026-08-20", (
            "a floor date inside the rolling window must win, same contract "
            "as allocation.hurdle's alloc_hurdle_since")


def test_floor_since_falls_back_to_the_rolling_window_when_unset():
    from tools.feature_edge_study import _floor_since
    from config import today_ist
    from datetime import timedelta
    with cfg_ctx({"priors_intraday_lookback_days": "90", "priors_intraday_since": ""}):
        expected = (today_ist() - timedelta(days=90)).isoformat()
        assert _floor_since(None) == expected


TESTS = [
    ("hour bucket boundaries", test_hour_bucket_boundaries),
    ("win rate excludes TIMEOUT from the denominator", test_win_rate_excludes_timeout_from_the_denominator),
    ("win rate is None with no TARGET or STOP", test_win_rate_is_none_with_no_target_or_stop),
    ("numeric split fires on a real separation", test_numeric_split_fires_on_a_real_separation),
    ("numeric split is silent with no relationship", test_numeric_split_is_silent_when_nothing_separates_the_groups),
    ("numeric split respects the sample floor", test_numeric_split_respects_the_sample_floor),
    ("numeric split ignores rows missing the feature", test_numeric_split_ignores_rows_missing_the_feature),
    ("numeric value tolerates meta shipped as a JSON string", test_numeric_value_tolerates_meta_shipped_as_a_json_string),
    ("numeric value reads confidence from the column, not meta", test_numeric_value_reads_confidence_from_the_column_not_meta),
    ("categorical split fires for a standout bucket", test_categorical_split_fires_for_a_standout_bucket),
    ("categorical split ignores a thin category", test_categorical_split_ignores_a_thin_category),
    ("categorical split's hour bucket reads ts, not meta", test_categorical_split_hour_bucket_reads_ts_not_meta),
    ("is_favourable true when hi win rate beats lo", test_is_favourable_true_when_hi_win_rate_beats_lo),
    ("is_favourable false when hi win rate trails lo", test_is_favourable_false_when_hi_win_rate_trails_lo),
    ("is_favourable falls back to mean_pct when win rate ties", test_is_favourable_falls_back_to_mean_pct_when_win_rate_ties),
    ("is_favourable is None when nothing can decide it", test_is_favourable_none_when_nothing_can_decide_it),
    ("is_favourable is None on an exact tie", test_is_favourable_none_on_an_exact_tie),
    ("validation_outcome confirms when directions agree", test_validation_outcome_confirms_when_directions_agree),
    ("validation_outcome rejects when directions disagree", test_validation_outcome_rejects_when_directions_disagree),
    ("validation_outcome is None for a pre-F50 placeholder value", test_validation_outcome_is_none_for_a_pre_f50_placeholder_value),
    ("validation_outcome is None for unclear", test_validation_outcome_is_none_for_unclear),
    ("validation_outcome is None when the fresh check cannot decide", test_validation_outcome_is_none_when_the_fresh_check_cannot_decide),
    ("validation_outcome regression: ORB/MID would now validate correctly", test_validation_outcome_regression_orb_mid_would_now_validate_correctly),
    ("target_key includes category so categories do not collide", test_target_key_includes_category_so_categories_do_not_collide),
    ("target_key for a numeric split has no category suffix", test_target_key_for_a_numeric_split_has_no_category_suffix),
    ("floor_since explicit override wins outright", test_floor_since_explicit_override_wins_outright),
    ("floor_since honours priors_intraday_since", test_floor_since_honours_priors_intraday_since_like_the_allocator_does),
    ("floor_since falls back to the rolling window when unset", test_floor_since_falls_back_to_the_rolling_window_when_unset),
]
