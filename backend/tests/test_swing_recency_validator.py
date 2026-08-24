"""
Track E, Stage E6 (docs/TRADEOS_ROADMAP.md) — the recency validator this
stage's own plan and F-68 Section 4 promised: `tools/swing_feature_edge_study
.py::validate_pending_swing()` re-checks PENDING `SWING/`-prefixed
FEATURE_FILTER proposals against fresh data instead of relying on
`tools/feature_edge_study.py::validate_pending()`, which can never
correctly reach a `SWING/` row (confirmed in this module's own docstring
— it only matches real intraday engine names).

`_validation_outcome` and `_refind` are pure and unit-tested directly.
`validate_pending_swing` itself is I/O-heavy (brain_proposals plus two
independent `signal_output_daily` windows per row) — same class of
function as `tools.health`'s DB-backed checks, which this project's own
convention verifies live rather than mocks; see `check_pending_fill_
duplicates`/`check_sector_concentration_risk` for the precedent. Live
proof: `python -m tools.swing_feature_edge_study --validate --dry-run`
against F-68's own 31 real PENDING findings.
"""

from __future__ import annotations


# ── _validation_outcome ──────────────────────────────────────────────────────

def test_validation_outcome_agrees():
    from tools.swing_feature_edge_study import _validation_outcome
    assert _validation_outcome("favourable", True) is True
    assert _validation_outcome("unfavourable", False) is True


def test_validation_outcome_disagrees():
    from tools.swing_feature_edge_study import _validation_outcome
    assert _validation_outcome("favourable", False) is False
    assert _validation_outcome("unfavourable", True) is False


def test_validation_outcome_none_on_unclear_current_value():
    """The F-50 bug this mirrors defending against: a row whose original
    direction was never confidently claimed ('unclear', or a legacy
    placeholder) must not be read as an implicit UNFAVOURABLE claim."""
    from tools.swing_feature_edge_study import _validation_outcome
    assert _validation_outcome("unclear", True) is None
    assert _validation_outcome(None, True) is None
    assert _validation_outcome("no feature-level filter", False) is None


def test_validation_outcome_none_on_unresolved_fresh_data():
    from tools.swing_feature_edge_study import _validation_outcome
    assert _validation_outcome("favourable", None) is None


# ── _refind ──────────────────────────────────────────────────────────────────

def _rows(n_lo, n_hi, feature="rsi_daily", lo_val=20.0, hi_val=80.0,
         category_feature=None, category=None, other_category="OTHER"):
    rows = []
    for _ in range(n_lo):
        r = {feature: lo_val, "outcome_category": "STOP", "outcome_return_pct": -3.0}
        if category_feature:
            r[category_feature] = other_category
        rows.append(r)
    for _ in range(n_hi):
        r = {feature: hi_val, "outcome_category": "TARGET", "outcome_return_pct": 5.0}
        if category_feature:
            r[category_feature] = category
        rows.append(r)
    return rows


def test_refind_numeric_reruns_the_same_feature():
    """25+25=50 rows, not 20+20: numeric_split() takes n//3 per side
    (extreme terciles, dropping the middle third) — 40//3=13 falls short
    of MIN_SEGMENT=15 even though 20 rows were provided on each side."""
    from tools.swing_feature_edge_study import _refind
    rows = _rows(25, 25)
    found = _refind(rows, "rsi_daily", None, min_segment=15)
    assert found is not None
    assert found["feature"] == "rsi_daily"
    assert found["kind"] == "numeric"


def test_refind_categorical_matches_only_the_requested_category():
    from tools.swing_feature_edge_study import _refind
    rows = _rows(20, 20, category_feature="sector", category="metals & mining")
    found = _refind(rows, "sector", "metals & mining", min_segment=15)
    assert found is not None
    assert found["category"] == "metals & mining"
    # A category this proposal is NOT about must not match, even if it
    # would independently clear the bar.
    assert _refind(rows, "sector", "chemicals", min_segment=15) is None


def test_refind_returns_none_below_the_sample_floor():
    from tools.swing_feature_edge_study import _refind
    rows = _rows(5, 5)   # below MIN_SEGMENT
    assert _refind(rows, "rsi_daily", None, min_segment=15) is None


TESTS = [
    ("validation outcome agrees", test_validation_outcome_agrees),
    ("validation outcome disagrees", test_validation_outcome_disagrees),
    ("validation outcome is None on an unclear original claim",
     test_validation_outcome_none_on_unclear_current_value),
    ("validation outcome is None when fresh data can't decide",
     test_validation_outcome_none_on_unresolved_fresh_data),
    ("_refind reruns the same numeric feature",
     test_refind_numeric_reruns_the_same_feature),
    ("_refind matches only the requested category",
     test_refind_categorical_matches_only_the_requested_category),
    ("_refind returns None below the sample floor",
     test_refind_returns_none_below_the_sample_floor),
]
