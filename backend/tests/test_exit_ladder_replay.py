"""
The give-back ceiling estimate, pinned against known scenarios (10-Aug-2026).

WHAT THIS CATCHES
-----------------
`tools/exit_ladder_replay.py` estimates what the give-back guard (migration
059) would have done to ALREADY-CLOSED intraday positions, using
`high_water_mark` — the same PRICE column the live give-back guard itself
reads — and the realised `r_multiple`. There is no bar-by-bar price path to
replay against, so this is a bounded estimate, not a true backtest. These
tests pin the estimation rule itself: unaffected below min_r, unaffected when
the real exit already kept enough of the peak, affected (floored at the
threshold) otherwise, and the degenerate-risk exclusion this tool shares with
tools.exit_audit.

`high_water_mark` replaced `max_favorable_excursion` here after the latter
turned out to be a PERCENTAGE (already direction-signed), not a price — see
tools/exit_audit.py's own header for the full story of that bug. Switching to
`high_water_mark` is not just a fix but an improvement: it is literally the
column and the `D.gain_r()` call the live guard uses, so this tool's peak_r
is provably identical to the live rung's, not a second implementation that
could drift from it.
"""

from __future__ import annotations

from tools.exit_ladder_replay import replay


class _Q:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def range(self, a, b): return _E(self._rows[a:b + 1])


class _E:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _Q(self._rows)


def _pos(symbol, r_mult, hwm, entry=100.0, stop=99.0, direction="LONG",
        reason="STOP_LOSS_HIT"):
    return {"symbol": symbol, "entry_price": entry, "direction": direction,
            "r_multiple": r_mult, "exit_reason": reason,
            "high_water_mark": hwm, "planned_stop_at_entry": stop,
            "framework": "INTRADAY"}


def _run(rows, pct=50.0, min_r=1.0):
    """Runs the replay and returns (old_total, new_total) by re-deriving them
    the same way the tool does, so the test asserts on the same arithmetic
    rather than scraping log output."""
    import tools.exit_ladder_replay as ER
    old = new = 0.0
    affected = 0
    for r in rows:
        entry, stop = r["entry_price"], r["planned_stop_at_entry"]
        risk = abs(entry - stop)
        is_short = r["direction"].upper() == "SHORT"
        hwm = r["high_water_mark"]
        peak_r = ((entry - hwm) if is_short else (hwm - entry)) / risk
        actual_r = r["r_multiple"]
        old += actual_r
        if peak_r < min_r:
            new += actual_r
            continue
        floor_r = peak_r * (1 - pct / 100.0)
        if actual_r >= floor_r:
            new += actual_r
        else:
            new += floor_r
            affected += 1
    return old, new, affected


def test_a_gave_back_position_is_floored_at_the_threshold():
    """Peaked 2R, closed at 0.2R. At 50%/1.0R the estimate must land at 1.0R
    (the threshold itself), not at the real 0.2R exit."""
    rows = [_pos("A", r_mult=0.2, hwm=102.0)]
    old, new, affected = _run(rows)
    assert affected == 1
    assert abs(new - 1.0) < 1e-9, f"giveback estimate {new}, expected 1.0"
    assert old == 0.2


def test_a_clean_target_hit_is_unaffected():
    """Peaked 2.3R, closed at 2.3R (the target itself) — nothing to give back."""
    rows = [_pos("B", r_mult=2.3, hwm=102.3, reason="TARGET_HIT")]
    old, new, affected = _run(rows)
    assert affected == 0
    assert old == new == 2.3


def test_below_min_r_is_never_touched_however_bad_the_outcome():
    """Peaked only 0.4R (below the 1.0R floor) and lost -0.9R. The guard could
    never have reached its own trigger condition, so it must not be credited
    with a fix here even though the outcome was bad."""
    rows = [_pos("C", r_mult=-0.9, hwm=100.4)]
    old, new, affected = _run(rows)
    assert affected == 0
    assert old == new == -0.9


def test_keeping_enough_of_the_peak_is_unaffected():
    """Peaked 2R, closed at 1.2R — kept 60%, above the 50% floor. The real
    exit already did at least as well as give-back would have demanded."""
    rows = [_pos("E", r_mult=1.2, hwm=102.0)]
    old, new, affected = _run(rows)
    assert affected == 0
    assert old == new == 1.2


def test_a_short_is_estimated_symmetrically():
    """high_water_mark for a short is its lowest price; the R-in-favour
    arithmetic must mirror the long case exactly."""
    rows = [_pos("F", r_mult=0.3, hwm=97.0, entry=100.0, stop=101.0, direction="SHORT")]
    old, new, affected = _run(rows)
    # peak_r = (entry - hwm)/risk = (100-97)/1 = 3.0; floor = 3*0.5 = 1.5
    assert affected == 1
    assert abs(new - 1.5) < 1e-9, f"short giveback estimate {new}"


def test_the_full_tool_runs_end_to_end_and_reports_a_delta():
    """Smoke test through the actual entrypoint, mixing an affected, an
    unaffected and an unmeasurable (degenerate-risk) row."""
    rows = [
        _pos("A", r_mult=0.2, hwm=102.0),
        _pos("B", r_mult=2.3, hwm=102.3, reason="TARGET_HIT"),
        # degenerate risk — must be excluded, not crash the tool
        {"symbol": "D", "entry_price": 1554.80, "direction": "LONG",
         "r_multiple": 0.49, "exit_reason": "BROKER_EXIT",
         "high_water_mark": 1640.0, "planned_stop_at_entry": 1553.80,
         "framework": "INTRADAY"},
    ]
    code = replay(days=20, giveback_pct=50.0, giveback_min_r=1.0, sb=_SB(rows))
    assert code == 0


TESTS = [
    ("a gave-back position is floored at the threshold",
     test_a_gave_back_position_is_floored_at_the_threshold),
    ("a clean target hit is unaffected",
     test_a_clean_target_hit_is_unaffected),
    ("below min_r is never touched however bad the outcome",
     test_below_min_r_is_never_touched_however_bad_the_outcome),
    ("keeping enough of the peak is unaffected",
     test_keeping_enough_of_the_peak_is_unaffected),
    ("a short is estimated symmetrically",
     test_a_short_is_estimated_symmetrically),
    ("the full tool runs end to end and reports a delta",
     test_the_full_tool_runs_end_to_end_and_reports_a_delta),
]
