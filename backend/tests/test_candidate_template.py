"""
intraday/candidate_template.py — Stage D6, 24-Aug-2026 (docs/TRADEOS_
ROADMAP.md, Track D, branch feat/intraday-evolution).

WHAT THIS COVERS
-----------------
FEATURE_TRANSLATORS: each of the 11 keys, matching tools/discover_
engines.py's own `feats` dict, against a live SymbolContext — true,
false, and "unknown" (missing data, must not read as false) for a
representative sample; the three gap-based ones exhaustively since they
share one helper. from_proposal(): the structured-evidence shape, the
Pass A rejection, the already-covered (GDB) rejection, the unrecognised-
feature rejection, the old-shape-evidence rejection — every one of these
returns (None, reason), never guesses. evaluate(): the generic VWAP-
reclaim trigger fires only with the filter true AND the live shape
present, direction is always LONG, stop comes from risk_from_structure(),
target from the configured R-multiple.
"""

from __future__ import annotations

from datetime import datetime

from tests import cfg_ctx


def _ctx(**over):
    from intraday.strategies.base import SymbolContext
    base = dict(symbol="TEST", ltp=100.0, bars=[])
    base.update(over)
    return SymbolContext(**base)


def _bar(t, o, h, l, c, v=1000.0):
    from intraday.strategies.base import Bar
    return Bar(datetime(2026, 8, 24, 10, t), o, h, l, c, v)


def _reclaim_bars(vwap=100.0):
    """4 bars: 3 below vwap (a real flush), the 4th crossing back above on
    its own close — the exact single-bar-reclaim shape evaluate() requires.
    Prices kept tight so risk_from_structure() does not need a widened
    cap to accept the resulting stop."""
    return [
        _bar(0, 99.6, 99.9, 99.4, 99.6),
        _bar(1, 99.6, 99.8, 99.5, 99.7),
        _bar(2, 99.7, 99.9, 99.6, 99.75),   # recent[-2], below vwap
        _bar(3, 99.75, 100.6, 99.7, 100.5),  # recent[-1], crosses above
    ]


# ── FEATURE_TRANSLATORS ─────────────────────────────────────────────────────

def test_gap_down_true_false_and_unknown():
    from intraday.candidate_template import FEATURE_TRANSLATORS
    f = FEATURE_TRANSLATORS["gap down > 1%"]
    assert f(_ctx(day_open=98.0, prev_close=100.0)) is True     # -2%
    assert f(_ctx(day_open=99.5, prev_close=100.0)) is False    # -0.5%
    assert f(_ctx(day_open=None, prev_close=100.0)) is None     # unknown, not False
    assert f(_ctx(day_open=98.0, prev_close=None)) is None


def test_gap_up_and_flat_open():
    from intraday.candidate_template import FEATURE_TRANSLATORS
    up = FEATURE_TRANSLATORS["gap up > 1%"]
    flat = FEATURE_TRANSLATORS["flat open +/-0.3%"]
    assert up(_ctx(day_open=102.0, prev_close=100.0)) is True
    assert up(_ctx(day_open=100.5, prev_close=100.0)) is False
    assert flat(_ctx(day_open=100.2, prev_close=100.0)) is True
    assert flat(_ctx(day_open=101.0, prev_close=100.0)) is False


def test_volume_ratio_features():
    from intraday.candidate_template import FEATURE_TRANSLATORS
    hi = FEATURE_TRANSLATORS["prior volume > 1.5x"]
    lo = FEATURE_TRANSLATORS["prior volume < 0.8x"]
    assert hi(_ctx(vol_ratio_daily=2.0)) is True
    assert hi(_ctx(vol_ratio_daily=1.0)) is False
    assert hi(_ctx(vol_ratio_daily=None)) is None
    assert lo(_ctx(vol_ratio_daily=0.5)) is True
    assert lo(_ctx(vol_ratio_daily=0.9)) is False
    assert lo(_ctx(vol_ratio_daily=0.0)) is False   # 0 < x < 0.8 excludes 0 itself


def test_adx_features():
    from intraday.candidate_template import FEATURE_TRANSLATORS
    trending = FEATURE_TRANSLATORS["ADX > 25 (trending)"]
    choppy = FEATURE_TRANSLATORS["ADX < 18 (choppy)"]
    assert trending(_ctx(adx_daily=30.0)) is True
    assert trending(_ctx(adx_daily=10.0)) is False
    assert trending(_ctx(adx_daily=None)) is None
    assert choppy(_ctx(adx_daily=12.0)) is True
    assert choppy(_ctx(adx_daily=30.0)) is False


def test_remaining_daily_indicator_features():
    from intraday.candidate_template import FEATURE_TRANSLATORS
    assert FEATURE_TRANSLATORS["ATR > 3% (volatile)"](_ctx(atr_pct_daily=4.0)) is True
    assert FEATURE_TRANSLATORS["ATR > 3% (volatile)"](_ctx(atr_pct_daily=None)) is None
    assert FEATURE_TRANSLATORS["delivery > 60%"](_ctx(delivery_pct_daily=70.0)) is True
    assert FEATURE_TRANSLATORS["delivery > 60%"](_ctx(delivery_pct_daily=None)) is None
    assert FEATURE_TRANSLATORS["RS vs NIFTY > 5"](_ctx(rs_vs_nifty_daily=8.0)) is True
    assert FEATURE_TRANSLATORS["RS vs NIFTY > 5"](_ctx(rs_vs_nifty_daily=None)) is None
    assert FEATURE_TRANSLATORS["extended > 8% o/50MA"](_ctx(dist_sma50_daily=10.0)) is True
    assert FEATURE_TRANSLATORS["extended > 8% o/50MA"](_ctx(dist_sma50_daily=None)) is None


def test_translators_cover_every_discover_engines_feature():
    """Regression pin: the 11 keys here must stay a byte-identical mirror
    of discover_engines.py's own `feats` dict names, or a future feature
    added there silently has no live translation."""
    from intraday.candidate_template import FEATURE_TRANSLATORS
    from tools.discover_engines import feats as discover_feats
    assert set(FEATURE_TRANSLATORS) == set(discover_feats)


# ── from_proposal() ─────────────────────────────────────────────────────────

def _row(target_key="UNSEEN/ADX > 25 (trending)", proposal_id=42,
        evidence=None, confidence=0.6):
    if evidence is None:
        evidence = {"summary": "x", "feature_name": "ADX > 25 (trending)",
                   "avg_move_pct": 3.5, "lift": 2.1, "n_miss": 12}
    return {"id": proposal_id, "target_key": target_key,
            "evidence": evidence, "confidence": confidence}


def test_from_proposal_builds_a_valid_candidate():
    from intraday.candidate_template import from_proposal
    cand, reason = from_proposal(_row())
    assert cand is not None
    assert reason == "ok"
    assert cand.feature_name == "ADX > 25 (trending)"
    assert cand.proposal_id == 42
    assert cand.avg_move_pct == 3.5
    assert cand.name == "CAND42"


def test_pass_a_subject_is_rejected():
    from intraday.candidate_template import from_proposal
    cand, reason = from_proposal(_row(target_key="BLOCKED_STRUCTURE/PDL"))
    assert cand is None
    assert "not a Pass B" in reason


def test_already_covered_gap_down_is_rejected():
    """gap down > 1% is GDB's own condition -- a second shadow engine for
    the identical population tests nothing new."""
    from intraday.candidate_template import from_proposal
    cand, reason = from_proposal(_row(target_key="UNSEEN/gap down > 1%",
                                      evidence={"summary": "x",
                                               "avg_move_pct": 5.0}))
    assert cand is None
    assert "GDB" in reason


def test_unrecognised_feature_name_is_rejected():
    from intraday.candidate_template import from_proposal
    cand, reason = from_proposal(_row(target_key="UNSEEN/some new feature"))
    assert cand is None
    assert "unrecognised" in reason.lower()


def test_old_shape_string_evidence_is_rejected_not_guessed():
    """A pre-Stage-D6 Pass B row (before evidence became structured) must
    be refused, never parsed by guessing at the sentence."""
    from intraday.candidate_template import from_proposal
    cand, reason = from_proposal(_row(evidence="after 'ADX > 25', 40% of..."))
    assert cand is None
    assert "structured shape" in reason


def test_missing_id_is_rejected():
    from intraday.candidate_template import from_proposal
    row = _row()
    row["id"] = None
    cand, reason = from_proposal(row)
    assert cand is None


# ── evaluate() ───────────────────────────────────────────────────────────────

def _candidate(feature_name="ADX > 25 (trending)"):
    from intraday.candidate_template import TemplatedCandidate
    return TemplatedCandidate(proposal_id=42, feature_name=feature_name,
                              avg_move_pct=3.5, confidence=0.55,
                              lift=2.1, n_miss=12)


def test_evaluate_fires_on_a_valid_reclaim():
    from intraday.candidate_template import evaluate
    with cfg_ctx({"candidate_lookback_bars": "12", "candidate_min_bars_below": "2",
                  "candidate_max_risk_pct": "5.0", "candidate_target_r": "2.0"}):
        ctx = _ctx(adx_daily=30.0, vwap=100.0, ltp=100.5, bars=_reclaim_bars())
        setup = evaluate(_candidate(), ctx, "OPENING")
    assert setup is not None
    assert setup.direction == "LONG"
    assert setup.strategy == "CAND42"
    assert setup.stop < setup.entry < setup.target
    assert setup.meta["feature_name"] == "ADX > 25 (trending)"
    assert setup.meta["template"] is True


def test_evaluate_refuses_when_filter_is_false():
    from intraday.candidate_template import evaluate
    ctx = _ctx(adx_daily=10.0, vwap=100.0, ltp=100.5, bars=_reclaim_bars())
    assert evaluate(_candidate(), ctx, "OPENING") is None


def test_evaluate_refuses_when_filter_is_unknown():
    """Missing data must not be treated as the condition being satisfied."""
    from intraday.candidate_template import evaluate
    ctx = _ctx(adx_daily=None, vwap=100.0, ltp=100.5, bars=_reclaim_bars())
    assert evaluate(_candidate(), ctx, "OPENING") is None


def test_evaluate_refuses_outside_opening_and_prime():
    from intraday.candidate_template import evaluate
    ctx = _ctx(adx_daily=30.0, vwap=100.0, ltp=100.5, bars=_reclaim_bars())
    assert evaluate(_candidate(), ctx, "DRIFT") is None


def test_evaluate_refuses_without_the_single_bar_crossing():
    """Price above VWAP for two bars running (a stale reclaim), not the
    fresh single-bar cross evaluate() requires."""
    from intraday.candidate_template import evaluate
    bars = _reclaim_bars()
    bars[-2] = _bar(2, 100.6, 100.8, 100.4, 100.6)   # already above vwap
    with cfg_ctx({"candidate_max_risk_pct": "5.0"}):
        ctx = _ctx(adx_daily=30.0, vwap=100.0, ltp=100.5, bars=bars)
        assert evaluate(_candidate(), ctx, "OPENING") is None


def test_evaluate_refuses_too_few_bars():
    from intraday.candidate_template import evaluate
    ctx = _ctx(adx_daily=30.0, vwap=100.0, ltp=100.5, bars=_reclaim_bars()[:2])
    assert evaluate(_candidate(), ctx, "OPENING") is None


def test_evaluate_never_produces_a_short():
    """Every candidate is LONG only, regardless of feature name -- see
    this module's own docstring for why direction is never inferred."""
    from intraday.candidate_template import evaluate
    with cfg_ctx({"candidate_max_risk_pct": "5.0"}):
        ctx = _ctx(adx_daily=30.0, vwap=100.0, ltp=100.5, bars=_reclaim_bars())
        setup = evaluate(_candidate(), ctx, "OPENING")
    assert setup.direction == "LONG"


def test_evaluate_target_uses_the_configured_r_multiple():
    from intraday.candidate_template import evaluate
    with cfg_ctx({"candidate_max_risk_pct": "5.0", "candidate_target_r": "3.0"}):
        ctx = _ctx(adx_daily=30.0, vwap=100.0, ltp=100.5, bars=_reclaim_bars())
        setup = evaluate(_candidate(), ctx, "OPENING")
    risk = setup.entry - setup.stop
    expected_target = round(setup.entry + risk * 3.0, 2)
    assert abs(setup.target - expected_target) < 0.05


TESTS = [
    ("gap down: true/false/unknown", test_gap_down_true_false_and_unknown),
    ("gap up and flat open", test_gap_up_and_flat_open),
    ("volume ratio features", test_volume_ratio_features),
    ("ADX features", test_adx_features),
    ("remaining daily-indicator features", test_remaining_daily_indicator_features),
    ("translators cover every discover_engines feature",
     test_translators_cover_every_discover_engines_feature),
    ("from_proposal builds a valid candidate", test_from_proposal_builds_a_valid_candidate),
    ("Pass A subject is rejected", test_pass_a_subject_is_rejected),
    ("already-covered gap-down is rejected", test_already_covered_gap_down_is_rejected),
    ("unrecognised feature name is rejected", test_unrecognised_feature_name_is_rejected),
    ("old-shape string evidence is rejected, not guessed",
     test_old_shape_string_evidence_is_rejected_not_guessed),
    ("missing id is rejected", test_missing_id_is_rejected),
    ("evaluate fires on a valid reclaim", test_evaluate_fires_on_a_valid_reclaim),
    ("evaluate refuses when filter is false", test_evaluate_refuses_when_filter_is_false),
    ("evaluate refuses when filter is unknown", test_evaluate_refuses_when_filter_is_unknown),
    ("evaluate refuses outside OPENING/PRIME", test_evaluate_refuses_outside_opening_and_prime),
    ("evaluate refuses without the single-bar crossing",
     test_evaluate_refuses_without_the_single_bar_crossing),
    ("evaluate refuses too few bars", test_evaluate_refuses_too_few_bars),
    ("evaluate never produces a short", test_evaluate_never_produces_a_short),
    ("evaluate target uses the configured R-multiple",
     test_evaluate_target_uses_the_configured_r_multiple),
]
