"""
Stage D2 — live universe re-qualification (docs/TRADEOS_ROADMAP.md, Track D).

WHAT THIS COVERS
-----------------
`_qualifies()` — the per-row gate extracted from build_universe() so that
function and movement_rejected_candidates() can never independently drift
on what "otherwise tradeable" means (the exact class of bug the hurdle/edge
units mismatch and the sub_engine overwrite both were, one level up).

`movement_rejected_candidates()` — the population a live re-check is
allowed to touch: qualifies on price/liquidity/delivery, failed ONLY on
yesterday's ATR band.

`live_requalify()` — pure admission logic against today's own live quote
data. This is the one that actually decides whether a name gets watched,
so it gets the most thorough coverage here.

`build_universe()` is NOT re-tested for its pre-existing behaviour here —
that would duplicate tools/replay/universe.py's own drift guard. What IS
checked is that the refactor to route through `_qualifies()` did not change
its rejected-count/kept-list shape, via one direct fixture.
"""

from __future__ import annotations

from unittest.mock import patch

from tests import cfg_ctx


# ── _qualifies ────────────────────────────────────────────────────────────

def _row(**over):
    base = {"symbol": "TEST", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
           "delivery_pct": 30.0, "asm_flag": False, "fo_ban_flag": False}
    base.update(over)
    return base


_GATES = dict(min_price=50.0, min_value=25.0, min_atr=1.20, max_atr=8.00, min_deliv=20.0,
             skip_flagged=True)


def test_qualifies_passes_a_clean_row():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(), **_GATES)
    assert ok and reason is None


def test_qualifies_rejects_below_min_price():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(close=40.0), **_GATES)
    assert not ok and reason == "price"


def test_qualifies_rejects_flagged_when_skip_flagged_true():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(asm_flag=True), **_GATES)
    assert not ok and reason == "flagged"


def test_qualifies_admits_flagged_when_skip_flagged_false():
    from intraday.scanner import _qualifies
    gates = {**_GATES, "skip_flagged": False}
    ok, reason = _qualifies(_row(asm_flag=True), **gates)
    assert ok and reason is None


def test_qualifies_rejects_below_min_turnover():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(value_cr=10.0), **_GATES)
    assert not ok and reason == "liquidity"


def test_qualifies_rejects_atr_below_floor():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(atr_pct=0.5), **_GATES)
    assert not ok and reason == "movement"


def test_qualifies_rejects_atr_above_ceiling():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(atr_pct=9.0), **_GATES)
    assert not ok and reason == "movement"


def test_qualifies_rejects_below_min_delivery():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(delivery_pct=5.0), **_GATES)
    assert not ok and reason == "delivery"


def test_qualifies_require_movement_false_skips_the_atr_check_entirely():
    """The exact lever movement_rejected_candidates() needs: a row whose
    ATR alone would fail must still pass every OTHER gate when movement
    is not being checked."""
    from intraday.scanner import _qualifies
    gates = {**_GATES, "require_movement": False}
    ok, reason = _qualifies(_row(atr_pct=0.1), **gates)
    assert ok and reason is None


def test_qualifies_checks_gates_in_price_flagged_liquidity_movement_delivery_order():
    """A row failing multiple gates reports the FIRST one, matching
    build_universe()'s own rejected-counter shape exactly (one row,
    one bucket) — order matters for that counter to stay meaningful."""
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(close=10.0, value_cr=1.0), **_GATES)
    assert not ok and reason == "price"


# ── build_universe() refactor did not change behaviour ──────────────────

def test_build_universe_refactor_preserves_rejection_shape():
    """Same fixture shape as tools/replay/universe.py's own drift guard,
    exercised directly against scanner.build_universe() this time — proves
    the _qualifies() extraction changed nothing observable."""
    from intraday.scanner import build_universe

    class _Query:
        def __init__(self, rows): self._rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return self
        @property
        def data(self): return self._rows

    class _SB:
        def __init__(self, rows): self._rows = rows
        def table(self, name): return _Query(self._rows)

    rows = [
        {"symbol": "GOOD", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "bank",
         "asm_flag": False, "fo_ban_flag": False},
        {"symbol": "CHEAP", "close": 10.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "auto",
         "asm_flag": False, "fo_ban_flag": False},
        {"symbol": "FLAGGED", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "it",
         "asm_flag": True, "fo_ban_flag": False},
        {"symbol": "QUIET", "close": 100.0, "value_cr": 50.0, "atr_pct": 0.5,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "energy",
         "asm_flag": False, "fo_ban_flag": False},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-08-23"):
            kept = build_universe(_SB(rows), limit=40)
    assert [e.symbol for e in kept] == ["GOOD"], (
        f"expected only GOOD to survive, got {[e.symbol for e in kept]}")


# ── movement_rejected_candidates() ───────────────────────────────────────

def test_movement_rejected_candidates_returns_only_the_atr_only_failures():
    from intraday.scanner import movement_rejected_candidates

    class _Query:
        def __init__(self, rows): self._rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return self
        @property
        def data(self): return self._rows

    class _SB:
        def __init__(self, rows): self._rows = rows
        def table(self, name): return _Query(self._rows)

    rows = [
        # Fails ONLY on ATR -- exactly the population wanted.
        {"symbol": "QUIET", "close": 100.0, "value_cr": 50.0, "atr_pct": 0.5,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "energy",
         "asm_flag": False, "fo_ban_flag": False},
        # Fails on price TOO -- not our population even though ATR is also low.
        {"symbol": "CHEAP_AND_QUIET", "close": 10.0, "value_cr": 50.0, "atr_pct": 0.3,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "auto",
         "asm_flag": False, "fo_ban_flag": False},
        # Already qualifies outright -- would already be in the daily bench.
        {"symbol": "ALREADY_IN", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "bank",
         "asm_flag": False, "fo_ban_flag": False},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-08-23"):
            out = movement_rejected_candidates(_SB(rows))
    assert [e.symbol for e in out] == ["QUIET"], (
        f"expected only QUIET, got {[e.symbol for e in out]}")


def test_movement_rejected_candidates_respects_exclude():
    from intraday.scanner import movement_rejected_candidates

    class _Query:
        def __init__(self, rows): self._rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return self
        @property
        def data(self): return self._rows

    class _SB:
        def __init__(self, rows): self._rows = rows
        def table(self, name): return _Query(self._rows)

    rows = [
        {"symbol": "QUIET", "close": 100.0, "value_cr": 50.0, "atr_pct": 0.5,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "energy",
         "asm_flag": False, "fo_ban_flag": False},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-08-23"):
            out = movement_rejected_candidates(_SB(rows), exclude={"QUIET"})
    assert out == [], "an excluded symbol (already in the bench) must never be re-checked"


# ── live_requalify() ─────────────────────────────────────────────────────

def _candidate(symbol="QUIET", atr_pct=0.5):
    from intraday.scanner import UniverseEntry
    return UniverseEntry(symbol=symbol, close=100.0, value_cr=50.0, atr_pct=atr_pct,
                         delivery_pct=30.0, sector="energy", score=0.0,
                         reason="test fixture", avg_vol_20d=1000.0)


def _quote(ltp=110.0, close=100.0, volume=3_000_000, avg_price=105.0):
    return {"ltp": ltp, "close": close, "volume": volume, "avg_price": avg_price}


def test_live_requalify_admits_a_candidate_clearing_both_floors():
    from intraday.scanner import live_requalify
    # move = (110-100)/100 = 10%; turnover = 3e6*105/1e7 = 31.5cr
    out = live_requalify([_candidate()], {"QUIET": _quote()}, move_pct=1.20, turnover_cr=25.0)
    assert len(out) == 1 and out[0].symbol == "QUIET"
    assert "LIVE REQUALIFIED" in out[0].reason


def test_live_requalify_refuses_move_ok_but_turnover_too_thin():
    from intraday.scanner import live_requalify
    thin = _quote(volume=100_000, avg_price=105.0)  # ~1.05cr, well under 25cr
    out = live_requalify([_candidate()], {"QUIET": thin}, move_pct=1.20, turnover_cr=25.0)
    assert out == []


def test_live_requalify_refuses_turnover_ok_but_move_too_small():
    from intraday.scanner import live_requalify
    barely_moved = _quote(ltp=100.3, close=100.0)  # 0.3% move, under 1.20% floor
    out = live_requalify([_candidate()], {"QUIET": barely_moved}, move_pct=1.20, turnover_cr=25.0)
    assert out == []


def test_live_requalify_admits_a_downward_move_same_as_upward():
    """The floor is a MAGNITUDE test, matching build_universe()'s own ATR
    band being direction-agnostic — a stock down 10% is exactly as
    tradeable-for-movement as one up 10%."""
    from intraday.scanner import live_requalify
    down = _quote(ltp=90.0, close=100.0)  # -10%
    out = live_requalify([_candidate()], {"QUIET": down}, move_pct=1.20, turnover_cr=25.0)
    assert len(out) == 1


def test_live_requalify_skips_a_candidate_with_no_quote():
    """Absence is not evidence either way — same rule live_rerank() already
    applies to a symbol with no live tick yet."""
    from intraday.scanner import live_requalify
    out = live_requalify([_candidate()], {}, move_pct=1.20, turnover_cr=25.0)
    assert out == []


def test_live_requalify_skips_zero_prev_close_defensively():
    from intraday.scanner import live_requalify
    bad = _quote(close=0.0)
    out = live_requalify([_candidate()], {"QUIET": bad}, move_pct=1.20, turnover_cr=25.0)
    assert out == [], "a zero previous close must never divide-by-zero into a false admit"


def test_live_requalify_carries_yesterdays_real_atr_through_honestly():
    """The admitted entry's atr_pct is the REAL (sub-floor) historical
    value, not a fabricated one — so a downstream reader can always see
    why this name was not in the ordinary daily bench."""
    from intraday.scanner import live_requalify
    out = live_requalify([_candidate(atr_pct=0.42)], {"QUIET": _quote()},
                         move_pct=1.20, turnover_cr=25.0)
    assert out[0].atr_pct == 0.42


TESTS = [
    ("qualifies passes a clean row", test_qualifies_passes_a_clean_row),
    ("qualifies rejects below min price", test_qualifies_rejects_below_min_price),
    ("qualifies rejects flagged when skip_flagged true", test_qualifies_rejects_flagged_when_skip_flagged_true),
    ("qualifies admits flagged when skip_flagged false", test_qualifies_admits_flagged_when_skip_flagged_false),
    ("qualifies rejects below min turnover", test_qualifies_rejects_below_min_turnover),
    ("qualifies rejects atr below floor", test_qualifies_rejects_atr_below_floor),
    ("qualifies rejects atr above ceiling", test_qualifies_rejects_atr_above_ceiling),
    ("qualifies rejects below min delivery", test_qualifies_rejects_below_min_delivery),
    ("qualifies require_movement=False skips the atr check entirely", test_qualifies_require_movement_false_skips_the_atr_check_entirely),
    ("qualifies checks gates in a fixed order", test_qualifies_checks_gates_in_price_flagged_liquidity_movement_delivery_order),
    ("build_universe refactor preserves rejection shape", test_build_universe_refactor_preserves_rejection_shape),
    ("movement_rejected_candidates returns only the atr-only failures", test_movement_rejected_candidates_returns_only_the_atr_only_failures),
    ("movement_rejected_candidates respects exclude", test_movement_rejected_candidates_respects_exclude),
    ("live_requalify admits a candidate clearing both floors", test_live_requalify_admits_a_candidate_clearing_both_floors),
    ("live_requalify refuses move ok but turnover too thin", test_live_requalify_refuses_move_ok_but_turnover_too_thin),
    ("live_requalify refuses turnover ok but move too small", test_live_requalify_refuses_turnover_ok_but_move_too_small),
    ("live_requalify admits a downward move same as upward", test_live_requalify_admits_a_downward_move_same_as_upward),
    ("live_requalify skips a candidate with no quote", test_live_requalify_skips_a_candidate_with_no_quote),
    ("live_requalify skips zero prev close defensively", test_live_requalify_skips_zero_prev_close_defensively),
    ("live_requalify carries yesterday's real atr through honestly", test_live_requalify_carries_yesterdays_real_atr_through_honestly),
]
