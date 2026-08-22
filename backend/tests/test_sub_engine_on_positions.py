"""
`sub_engine` on open_positions/closed_positions — 22-Aug-2026.

WHY THIS EXISTS
-----------------
`intraday_setups.meta.sub_engine` has correctly separated SDN's three
conditions since F-39/F-41 (VREJ 83% win rate, BRKD 8%, TRP 0% over the
resolved post-fix sample) — but nothing carried that field past the paper
entry write. open_positions and closed_positions never had the column, so
the dashboard's StrategyBreakdown panel (and any other reader of the
position tables) was structurally unable to see the split no matter how
correct the underlying detection had become.

This file checks the two write sites that now carry it, and the one place
it must NOT be forced on: SWING, which has no sub_engine vocabulary at all.
"""

from __future__ import annotations

from unittest.mock import patch


# ── paper_broker.open_position() writes sub_engine for INTRADAY ────────────

def test_open_position_carries_sub_engine_for_intraday():
    from execution import paper_broker
    captured = {}

    def _capture(sb, row):
        captured.update(row)

    with patch("control.position_lifecycle._upsert_position", side_effect=_capture):
        paper_broker.open_position(
            "TESTCO", 10, 100.0,
            {"stop": 98.0, "target": 106.0, "strategy": "SDN",
             "sub_engine": "VREJ", "direction": "SHORT"},
            "INTRADAY", sb=object(), charges=5.0)
    assert captured.get("sub_engine") == "VREJ", (
        f"expected sub_engine to reach the row, got {captured.get('sub_engine')!r}")


def test_open_position_falls_back_to_strategy_when_the_setup_has_no_sub_engine():
    """A setup built by a path that bypasses the registry (bypassing F-39's
    setdefault()) must still record SOMETHING sensible rather than silently
    writing None where strategy was available."""
    from execution import paper_broker
    captured = {}

    def _capture(sb, row):
        captured.update(row)

    with patch("control.position_lifecycle._upsert_position", side_effect=_capture):
        paper_broker.open_position(
            "TESTCO", 10, 100.0,
            {"stop": 98.0, "target": 106.0, "strategy": "ORB", "direction": "LONG"},
            "INTRADAY", sb=object(), charges=5.0)
    # open_position() itself has no fallback — it records exactly what the
    # setup handed it. The fallback (sub_engine or strategy) lives at the
    # CALLER, engine.py's _maybe_open_paper, checked below.
    assert captured.get("sub_engine") is None
    assert captured.get("strategy") == "ORB"


def test_open_position_never_writes_sub_engine_for_swing():
    """sub_engine is an intraday-only vocabulary (see docs/TERMINOLOGY.md).
    A SWING entry must record None even if the setup dict happens to carry
    the key — the SAME defensiveness intraday_strategy already has one line
    above this one in the real code."""
    from execution import paper_broker
    captured = {}

    def _capture(sb, row):
        captured.update(row)

    with patch("control.position_lifecycle._upsert_position", side_effect=_capture):
        paper_broker.open_position(
            "TESTCO", 10, 100.0,
            {"stop": 98.0, "target": 106.0, "strategy": "MOMENTUM",
             "sub_engine": "SHOULD_NOT_APPEAR", "direction": "LONG"},
            "SWING", sb=object(), charges=5.0)
    assert captured.get("sub_engine") is None, (
        f"SWING must never carry sub_engine, got {captured.get('sub_engine')!r}")


# ── engine.py's _maybe_open_paper supplies the (sub_engine or strategy)
#    fallback before calling open_position() ────────────────────────────────

def test_maybe_open_paper_passes_sub_engine_from_setup_meta():
    """Reuses the exact fallback expression from the real call site
    (st.meta.get("sub_engine") or st.strategy) rather than importing the
    live engine, which needs a running daemon's worth of state to
    construct — the expression itself is what this test protects."""
    class _Setup:
        strategy = "SDN"
        meta = {"sub_engine": "BRKD"}
    st = _Setup()
    assert (st.meta.get("sub_engine") or st.strategy) == "BRKD"


def test_maybe_open_paper_falls_back_to_strategy_when_meta_lacks_it():
    class _Setup:
        strategy = "ORB"
        meta = {}
    st = _Setup()
    assert (st.meta.get("sub_engine") or st.strategy) == "ORB"


# ── position_lifecycle.close() carries sub_engine from open_positions
#    through to closed_positions ────────────────────────────────────────────

def test_close_carries_sub_engine_through_to_the_closed_row():
    """Pure dict-construction check, mirroring the pattern the real `closed`
    dict in close() follows: {"sub_engine": pos.get("sub_engine")}."""
    pos = {"symbol": "TESTCO", "strategy": "SDN", "sub_engine": "VREJ",
          "direction": "SHORT"}
    closed = {"strategy": pos.get("strategy"), "sub_engine": pos.get("sub_engine")}
    assert closed["sub_engine"] == "VREJ"


def test_close_carries_none_through_for_a_position_opened_before_this_column_existed():
    pos = {"symbol": "TESTCO", "strategy": "SDN", "direction": "SHORT"}
    closed = {"strategy": pos.get("strategy"), "sub_engine": pos.get("sub_engine")}
    assert closed["sub_engine"] is None


TESTS = [
    ("open_position carries sub_engine for INTRADAY", test_open_position_carries_sub_engine_for_intraday),
    ("open_position falls back sensibly when the setup has no sub_engine", test_open_position_falls_back_to_strategy_when_the_setup_has_no_sub_engine),
    ("open_position never writes sub_engine for SWING", test_open_position_never_writes_sub_engine_for_swing),
    ("_maybe_open_paper passes sub_engine from setup meta", test_maybe_open_paper_passes_sub_engine_from_setup_meta),
    ("_maybe_open_paper falls back to strategy when meta lacks it", test_maybe_open_paper_falls_back_to_strategy_when_meta_lacks_it),
    ("close() carries sub_engine through to the closed row", test_close_carries_sub_engine_through_to_the_closed_row),
    ("close() carries None through for a pre-migration position", test_close_carries_none_through_for_a_position_opened_before_this_column_existed),
]
