"""
`_trend_ctx` was populated only once a day, at EOD, by manage_open_positions()
— intraday/engine.py's live 15s loop called evaluate_exit() every cycle, all
session, with a policy dict that never carried the key. So target_decision()'s
runner conversion and deterioration_check()'s "thesis broke while in profit"
exit were both blind for the entire trading day, every day. Neither fired
INCORRECTLY (TrendQuality.has_evidence requires checks >= 3, so zero evidence
correctly banks rather than false-running), but neither could fire AT ALL
during market hours.

Two things under test:

1. assess_trend() gains one genuinely-live vote — `dist_vwap_live` — sourced
   from the tick VWAP apply_live_quotes() already overlays onto self._contexts
   every 15s, not a recomputation. Absent entirely when the caller doesn't
   attach it, so this must not change behaviour for any EOD caller
   (manage_open_positions, tools/simulate.py) that never sets this key.

2. IntradayEngine.refresh_trend_context() is the live wiring:
   - default OFF must touch the database ZERO times (a stub that raises on
     .table() proves it, not just "returns without error");
   - when armed, it must call load_signal_context() (reused verbatim, not
     reimplemented) and merge the result into self._policy["_trend_ctx"],
     which is the exact field evaluate_exit() reads at engine.py's live
     call site;
   - it must attach dist_vwap_live from self._contexts, computed correctly.
"""

from __future__ import annotations

from unittest.mock import patch

from tests import cfg_ctx


# ── assess_trend(): the new live vote ────────────────────────────────────────

def test_live_price_above_vwap_is_a_point_for():
    from control.exit_rules import assess_trend
    tq = assess_trend({"dist_vwap_live": 0.8})
    assert tq.checks == 1, f"expected exactly one check counted, got {tq.checks}"
    assert any("above session VWAP" in r for r in tq.reasons), tq.reasons


def test_live_price_below_vwap_is_a_point_against():
    from control.exit_rules import assess_trend
    tq = assess_trend({"dist_vwap_live": -0.6})
    assert tq.checks == 1
    assert any("below session VWAP" in r for r in tq.against), tq.against


def test_absent_live_vwap_does_not_change_existing_behaviour():
    """
    EOD callers (manage_open_positions, tools/simulate.py) never attach
    dist_vwap_live. A sig dict shaped exactly like load_signal_context()'s
    output today must score identically to before this change.
    """
    from control.exit_rules import assess_trend
    sig = {"dist_sma50": 3.2, "rsi_daily": 61.0, "momentum_state": "HOT"}
    tq = assess_trend(dict(sig))
    assert tq.checks == 3, f"expected 3 checks from the existing fields only, got {tq.checks}"


def test_zero_evidence_plus_a_stray_none_vwap_still_abstains():
    """A dict carrying dist_vwap_live=None (e.g. VWAP unavailable this cycle
    for a bench-only context) must not be counted as a check."""
    from control.exit_rules import assess_trend
    tq = assess_trend({"dist_vwap_live": None})
    assert tq.checks == 0
    assert tq.verdict == "INTACT"
    assert not tq.has_evidence


# ── refresh_trend_context(): the live wiring ────────────────────────────────

class _NoDB:
    """Raises if anything touches the database — proves the default-off
    switch does not merely 'behave correctly', it never queries at all."""
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def _engine(positions):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine(sb=_NoDB())
    eng.positions = positions
    eng._policy = {"target_r": 3.0}  # a minimal real policy dict, as load_exit_policy() would hand back
    return eng


_POSITIONS = [
    {"symbol": "TCS", "framework": "SWING"},
    {"symbol": "SAPPHIRE", "framework": "SWING"},
    {"symbol": "AEGISLOG", "framework": "INTRADAY"},  # must be excluded
]


def test_default_off_touches_no_database_and_leaves_trend_ctx_empty():
    eng = _engine(_POSITIONS)
    n = eng.refresh_trend_context()
    assert n == 0
    assert eng._trend_ctx == {}
    # evaluate_exit() reads this as `policy.get("_trend_ctx") or {}` — None
    # and {} are the same contract to that caller, so assert on falsiness,
    # not on the early-return branch happening to (not) have set the key.
    assert not eng._policy.get("_trend_ctx")


def test_armed_calls_load_signal_context_for_swing_symbols_only():
    eng = _engine(_POSITIONS)
    captured = {}

    def _fake_load_signal_context(sb, symbols):
        captured["symbols"] = symbols
        return {s: {"momentum_state": "HOT"} for s in symbols}

    with cfg_ctx({"exit_live_trend_ctx": "true"}):
        with patch("control.exit_rules.load_signal_context", _fake_load_signal_context):
            n = eng.refresh_trend_context()

    assert captured["symbols"] == ["SAPPHIRE", "TCS"], (
        "must query exactly the held SWING symbols, sorted, excluding the "
        f"INTRADAY position — got {captured.get('symbols')}")
    assert n == 2
    # This is the field evaluate_exit() actually reads at its live call site.
    assert eng._policy["_trend_ctx"] is eng._trend_ctx
    assert eng._policy["_trend_ctx"]["TCS"]["momentum_state"] == "HOT"


def test_armed_attaches_live_vwap_distance_from_contexts():
    from intraday.strategies.base import SymbolContext
    eng = _engine(_POSITIONS)
    eng._contexts = {
        "TCS": SymbolContext(symbol="TCS", ltp=3210.0, bars=[], vwap=3200.0),
    }

    def _fake_load_signal_context(sb, symbols):
        return {s: {} for s in symbols}

    with cfg_ctx({"exit_live_trend_ctx": "true"}):
        with patch("control.exit_rules.load_signal_context", _fake_load_signal_context):
            eng.refresh_trend_context()

    got = eng._trend_ctx["TCS"]["dist_vwap_live"]
    expected = round((3210.0 - 3200.0) / 3200.0 * 100.0, 3)
    assert got == expected, f"expected {expected}, got {got}"
    # SAPPHIRE has no live context this cycle — must not raise, must not
    # fabricate a value.
    assert "dist_vwap_live" not in eng._trend_ctx["SAPPHIRE"]


def test_load_signal_context_failure_abstains_rather_than_crashing():
    eng = _engine(_POSITIONS)

    def _boom(sb, symbols):
        raise RuntimeError("supabase unreachable")

    with cfg_ctx({"exit_live_trend_ctx": "true"}):
        with patch("control.exit_rules.load_signal_context", _boom):
            n = eng.refresh_trend_context()

    assert n == 0
    assert eng._trend_ctx == {}


TESTS = [
    ("live price above VWAP is a point for",
     test_live_price_above_vwap_is_a_point_for),
    ("live price below VWAP is a point against",
     test_live_price_below_vwap_is_a_point_against),
    ("absent live VWAP does not change existing behaviour",
     test_absent_live_vwap_does_not_change_existing_behaviour),
    ("a stray None live VWAP still abstains",
     test_zero_evidence_plus_a_stray_none_vwap_still_abstains),
    ("default off touches no database",
     test_default_off_touches_no_database_and_leaves_trend_ctx_empty),
    ("armed queries SWING symbols only",
     test_armed_calls_load_signal_context_for_swing_symbols_only),
    ("armed attaches live VWAP distance",
     test_armed_attaches_live_vwap_distance_from_contexts),
    ("load_signal_context failure abstains, does not crash",
     test_load_signal_context_failure_abstains_rather_than_crashing),
]
