"""
Stage E2, Track E (docs/TRADEOS_ROADMAP.md) — tools/swing_feature_edge_study.py.

Pure-function coverage only, same boundary tools/feature_edge_study.py's own
tests draw: the tercile/significance/dedup-key machinery is fully
unit-testable; `_rows()`/`_propose()` are I/O and deliberately not tested
here. Live run, 24-Aug-2026, full history: CONTINUATION n=427 (20 findings),
MOM n=118 (11 findings), RVS n=12 (below the 40 floor, skipped) — real
numbers, not fixtures, cited in docs/FINDINGS.md.
"""

from __future__ import annotations


def _row(outcome_category, outcome_return_pct=None, **feats):
    r = {"outcome_category": outcome_category, "outcome_return_pct": outcome_return_pct}
    r.update(feats)
    return r


# ── _win_rate / _mean_pct ────────────────────────────────────────────────────

def test_win_rate_counts_target_and_stop_only():
    from tools.swing_feature_edge_study import _win_rate
    rows = [_row("TARGET"), _row("TARGET"), _row("STOP")]
    wr, t, s, o = _win_rate(rows)
    assert (wr, t, s, o) == (2 / 3, 2, 1, 0)


def test_win_rate_none_when_no_target_or_stop():
    from tools.swing_feature_edge_study import _win_rate
    wr, t, s, o = _win_rate([])
    assert wr is None and t == 0 and s == 0 and o == 0


# ── numeric_split ─────────────────────────────────────────────────────────────

def test_numeric_split_finds_a_real_gap():
    """Bottom third all STOP, top third all TARGET — must fire clean."""
    from tools.swing_feature_edge_study import numeric_split
    rows = ([_row("STOP", -3.0, rsi_daily=v) for v in range(1, 16)]
            + [_row("TARGET", 4.0, rsi_daily=v) for v in range(30, 45)]
            + [_row("TARGET", 2.0, rsi_daily=v) for v in range(50, 65)])  # middle third
    found = numeric_split(rows, "rsi_daily", min_segment=15)
    assert found is not None, "a clean 100%-vs-0%-win-rate split must fire"
    assert found["lo_win_rate"] == 0.0 and found["hi_win_rate"] == 1.0
    assert found["feature"] == "rsi_daily" and found["kind"] == "numeric"


def test_numeric_split_none_below_the_sample_floor():
    from tools.swing_feature_edge_study import numeric_split
    rows = [_row("STOP", -3.0, rsi_daily=v) for v in range(1, 5)]
    assert numeric_split(rows, "rsi_daily", min_segment=15) is None


def test_numeric_split_none_when_the_gap_is_too_small():
    """Both thirds close to the same win rate — must not fire on noise."""
    from tools.swing_feature_edge_study import numeric_split
    rows = ([_row("TARGET" if i % 2 == 0 else "STOP", 1.0, rsi_daily=i)
            for i in range(60)])
    assert numeric_split(rows, "rsi_daily", min_segment=15) is None


def test_numeric_split_ignores_rows_missing_the_feature():
    from tools.swing_feature_edge_study import numeric_split
    # 45 rows carry rsi_daily (third = 15, clears the floor); another 20
    # carry none at all and must not be counted toward either segment or
    # change which rows land in the bottom/top third.
    rows = ([_row("STOP", -3.0, rsi_daily=v) for v in range(1, 21)]
            + [_row("TARGET", 4.0, rsi_daily=v) for v in range(50, 75)]
            + [_row("TARGET", 2.0)   # no rsi_daily at all
               for _ in range(20)])
    found = numeric_split(rows, "rsi_daily", min_segment=15)
    assert found is not None, "45 valued rows must clear the sample floor"
    assert found["lo_n"] + found["hi_n"] <= 45, (
        "rows with no value for the feature must not be counted toward "
        "either segment")


# ── categorical_splits ───────────────────────────────────────────────────────

def test_categorical_split_fires_for_a_standout_category():
    from tools.swing_feature_edge_study import categorical_splits
    rows = ([_row("STOP", -3.0, sector="metals") for _ in range(11)]
            + [_row("TARGET", 4.0, sector="auto") for _ in range(30)]
            + [_row("STOP", -1.0, sector="auto") for _ in range(5)])
    out = categorical_splits(rows, "sector", min_segment=5)
    metals = [f for f in out if f.get("category") == "metals"]
    assert metals, "metals (0% win rate) must stand out against the rest"
    assert metals[0]["hi_win_rate"] == 0.0


def test_categorical_split_skips_a_category_below_the_floor():
    from tools.swing_feature_edge_study import categorical_splits
    rows = ([_row("STOP", -3.0, sector="metals") for _ in range(2)]  # too thin
            + [_row("TARGET", 4.0, sector="auto") for _ in range(30)])
    out = categorical_splits(rows, "sector", min_segment=15)
    assert not any(f.get("category") == "metals" for f in out)


# ── target_key_for / is_favourable ──────────────────────────────────────────

def test_target_key_is_swing_prefixed_and_never_collides_with_intraday():
    from tools.swing_feature_edge_study import target_key_for
    key = target_key_for("CONTINUATION", {"feature": "rsi_daily"})
    assert key == "SWING/CONTINUATION/rsi_daily"
    assert key.startswith("SWING/"), (
        "must be namespaced so this can never collide with an intraday "
        "engine's own target_key in the shared brain_proposals table")


def test_target_key_includes_category_for_categorical_findings():
    from tools.swing_feature_edge_study import target_key_for
    key = target_key_for("CONTINUATION", {"feature": "sector", "category": "metals & mining"})
    assert key == "SWING/CONTINUATION/sector/metals & mining"


def test_is_favourable_reads_win_rate_first():
    from tools.swing_feature_edge_study import is_favourable
    assert is_favourable({"hi_win_rate": 0.8, "lo_win_rate": 0.3,
                          "hi_mean_pct": None, "lo_mean_pct": None}) is True
    assert is_favourable({"hi_win_rate": 0.2, "lo_win_rate": 0.7,
                          "hi_mean_pct": None, "lo_mean_pct": None}) is False


def test_is_favourable_none_when_undecidable():
    from tools.swing_feature_edge_study import is_favourable
    assert is_favourable({"hi_win_rate": None, "lo_win_rate": None,
                          "hi_mean_pct": None, "lo_mean_pct": None}) is None


# ── engine_key ────────────────────────────────────────────────────────────────

def test_engine_key_uses_swing_family_and_groups_combos_correctly():
    """Read-only dependency on allocation.scoring.swing_family — the same
    import F-46 already established as safe. A MOM+SEC combo must group
    under MOM (the isolated family wins), matching swing_family()'s own
    documented rule."""
    from tools.swing_feature_edge_study import engine_key
    assert engine_key({"strategy": "MOM+SEC"}) == "MOM"
    assert engine_key({"strategy": "CTL+SEC"}) == "CONTINUATION"
    assert engine_key({"strategy": "RVS"}) == "RVS"


TESTS = [
    ("win rate counts target and stop only", test_win_rate_counts_target_and_stop_only),
    ("win rate none when no target or stop", test_win_rate_none_when_no_target_or_stop),
    ("numeric split finds a real gap", test_numeric_split_finds_a_real_gap),
    ("numeric split none below the sample floor", test_numeric_split_none_below_the_sample_floor),
    ("numeric split none when the gap is too small", test_numeric_split_none_when_the_gap_is_too_small),
    ("numeric split ignores rows missing the feature", test_numeric_split_ignores_rows_missing_the_feature),
    ("categorical split fires for a standout category", test_categorical_split_fires_for_a_standout_category),
    ("categorical split skips a category below the floor", test_categorical_split_skips_a_category_below_the_floor),
    ("target key is swing-prefixed and never collides with intraday",
     test_target_key_is_swing_prefixed_and_never_collides_with_intraday),
    ("target key includes category for categorical findings",
     test_target_key_includes_category_for_categorical_findings),
    ("is_favourable reads win rate first", test_is_favourable_reads_win_rate_first),
    ("is_favourable none when undecidable", test_is_favourable_none_when_undecidable),
    ("engine_key uses swing_family and groups combos correctly",
     test_engine_key_uses_swing_family_and_groups_combos_correctly),
]
