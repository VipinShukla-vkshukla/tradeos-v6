"""
tools/event_core_compare.py::compare_day() — Gate D3's own comparison
metric (docs/TRADEOS_ROADMAP.md, Track D, Stage D3).
"""

from __future__ import annotations


class _Query:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def order(self, *a, **k): return self
    def range(self, *a, **k): return self   # fixtures stay well under fetch_all()'s 1000-row page
    def execute(self): return self
    @property
    def data(self): return self._rows


class _SB:
    def __init__(self, by_table: dict[str, list[dict]]):
        self._by_table = by_table

    def table(self, name):
        return _Query(self._by_table.get(name, []))


def _shadow(symbol="TEST", sub_engine="ORB", direction="LONG", detected_at="2026-08-24T10:00:00+00:00"):
    return {"symbol": symbol, "sub_engine": sub_engine, "direction": direction,
           "detected_at": detected_at}


def _setup(symbol="TEST", strategy="ORB", direction="LONG", detected_at="2026-08-24T10:00:10+00:00",
          sub_engine=None):
    return {"symbol": symbol, "strategy": strategy, "direction": direction,
           "detected_at": detected_at,
           "meta": {"sub_engine": sub_engine or strategy}}


def test_matches_the_same_symbol_and_engine_within_the_window():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow()],
        "intraday_setups": [_setup()],   # 10s later, well inside 60s window
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 1
    assert r.shadow_only == 0


def test_computes_a_positive_latency_gap_when_shadow_was_sooner():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow(detected_at="2026-08-24T10:00:00+00:00")],
        "intraday_setups": [_setup(detected_at="2026-08-24T10:00:13+00:00")],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 1
    assert abs(r.latency_gaps_s[0] - 13.0) < 1e-6


def test_shadow_only_when_no_trusted_row_exists_within_the_window():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow()],
        "intraday_setups": [],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 0
    assert r.shadow_only == 1


def test_outside_the_window_counts_as_shadow_only_not_matched():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow(detected_at="2026-08-24T10:00:00+00:00")],
        "intraday_setups": [_setup(detected_at="2026-08-24T10:05:00+00:00")],  # 300s later
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 0
    assert r.shadow_only == 1


def test_different_symbol_never_matches():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow(symbol="A")],
        "intraday_setups": [_setup(symbol="B", detected_at="2026-08-24T10:00:05+00:00")],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 0
    assert r.shadow_only == 1


def test_different_sub_engine_never_matches():
    """The exact class of bug this project's own prior-key history warns
    about -- ORB and VWR firing on the same symbol at the same moment
    must not be treated as the same observation."""
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow(sub_engine="ORB")],
        "intraday_setups": [_setup(sub_engine="VWR", detected_at="2026-08-24T10:00:05+00:00")],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 0
    assert r.shadow_only == 1


def test_a_trusted_row_with_no_detected_at_is_excluded_not_treated_as_a_miss():
    """Migration 106 only stamps detected_at going forward -- a
    pre-migration row must be excluded from matching entirely, not
    counted as evidence the trusted loop missed something."""
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow()],
        "intraday_setups": [_setup(detected_at=None)],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 0
    assert r.shadow_only == 1


def test_matched_pair_with_different_direction_is_flagged_as_a_disagreement():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow(direction="LONG")],
        "intraday_setups": [_setup(direction="SHORT", detected_at="2026-08-24T10:00:05+00:00")],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 1
    assert len(r.disagreements) == 1


def test_closest_candidate_wins_when_multiple_are_within_the_window():
    from tools.event_core_compare import compare_day
    sb = _SB({
        "intraday_event_shadow": [_shadow(detected_at="2026-08-24T10:00:00+00:00")],
        "intraday_setups": [
            _setup(detected_at="2026-08-24T10:00:50+00:00"),
            _setup(detected_at="2026-08-24T10:00:05+00:00"),   # closest
        ],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 1
    assert abs(r.latency_gaps_s[0] - 5.0) < 1e-6


def test_meta_stored_as_json_string_is_read_correctly():
    """Same defensive read allocation/scoring.py::_engine_of() already
    needs -- some historical rows carry meta as a JSON string, not object."""
    from tools.event_core_compare import compare_day
    import json
    setup = _setup(sub_engine="ORB", detected_at="2026-08-24T10:00:05+00:00")
    setup["meta"] = json.dumps(setup["meta"])
    sb = _SB({
        "intraday_event_shadow": [_shadow(sub_engine="ORB")],
        "intraday_setups": [setup],
    })
    r = compare_day(sb, "2026-08-24", window_s=60)
    assert r.matched == 1


TESTS = [
    ("matches the same symbol and engine within the window", test_matches_the_same_symbol_and_engine_within_the_window),
    ("computes a positive latency gap when shadow was sooner", test_computes_a_positive_latency_gap_when_shadow_was_sooner),
    ("shadow-only when no trusted row exists within the window", test_shadow_only_when_no_trusted_row_exists_within_the_window),
    ("outside the window counts as shadow-only, not matched", test_outside_the_window_counts_as_shadow_only_not_matched),
    ("different symbol never matches", test_different_symbol_never_matches),
    ("different sub_engine never matches", test_different_sub_engine_never_matches),
    ("a trusted row with no detected_at is excluded, not treated as a miss", test_a_trusted_row_with_no_detected_at_is_excluded_not_treated_as_a_miss),
    ("matched pair with different direction is flagged as a disagreement", test_matched_pair_with_different_direction_is_flagged_as_a_disagreement),
    ("closest candidate wins when multiple are within the window", test_closest_candidate_wins_when_multiple_are_within_the_window),
    ("meta stored as JSON string is read correctly", test_meta_stored_as_json_string_is_read_correctly),
]
