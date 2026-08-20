"""
F-46, 21-Aug-2026 — family-calibrated stall clock + continuous trend
telemetry, closing the gap the F-43 review named: the swing exit ladder
predicts nothing about an open position's own likely pace, and the one
signal that reads like a prediction (assess_trend's STRONG/INTACT/BROKEN)
had never once been graded against a resolved trade.

Real numbers this session measured from every plan swing/signals/
outcomes.py has resolved as TARGET:

    family          n     median days-to-target   p75
    CONTINUATION   296             3                6
    MOM             86             4                7
    RVS              6         (too thin for its own number)

`_calibrate()` is pure (I/O separated exactly like intraday_priors(sb,
rows=...)), so these are exercised directly with fixture rows rather than
a live fetch. `evaluate_exit()`'s own use of the calibration is exercised
with a CTL-labelled position that the flat 10-day default would still be
holding, and the calibrated 6-day clock correctly stalls.
"""

from __future__ import annotations

from tests import cfg_ctx


def _family_fn(strategy: str | None) -> str:
    """Minimal stand-in for allocation.scoring.swing_family, isolating this
    test from that module the same way the production code's own injection
    point (`family_fn` argument) is designed to allow."""
    s = (strategy or "")
    if "MOM" in s or "RVS" in s:
        return "MOM" if "MOM" in s else "RVS"
    return "CONTINUATION" if s else "ALL"


# ── 1. _calibrate() — pure, no I/O ──────────────────────────────────────────

def test_calibrate_matches_the_measured_continuation_and_mom_numbers():
    from swing.signals.pace_calibration import _calibrate
    rows = ([{"strategy": "CTL", "outcome_hold_days": d}
             for d in ([2, 3] * 100 + [6] * 96)]  # median~2-3, p75 exactly 6
            + [{"strategy": "MOM", "outcome_hold_days": d}
               for d in ([4] * 74 + [7] * 26)])  # sorted: p75 index 74 -> 7
    out = _calibrate(rows, _family_fn, min_sample=20,
                     global_default=10, floor_days=3)
    assert out["CONTINUATION"] == 6, (
        f"CONTINUATION's own p75 must calibrate to 6, got {out.get('CONTINUATION')}")
    assert out["MOM"] == 7, (
        f"MOM's own p75 must calibrate to 7, got {out.get('MOM')}")


def test_calibrate_drops_a_family_below_the_sample_floor():
    from swing.signals.pace_calibration import _calibrate
    rows = [{"strategy": "RVS", "outcome_hold_days": 1} for _ in range(6)]
    out = _calibrate(rows, _family_fn, min_sample=20,
                     global_default=10, floor_days=3)
    assert "RVS" not in out, (
        "6 observations must not produce a trusted calibration — the "
        "caller's flat default must remain the answer for this family")


def test_calibrate_never_exceeds_the_global_default():
    """A family whose winners take LONGER than the book default must be
    left AT the default, never given more rope — see the module's own
    'tighten, never loosen' contract."""
    from swing.signals.pace_calibration import _calibrate
    rows = [{"strategy": "MOM", "outcome_hold_days": 25} for _ in range(30)]
    out = _calibrate(rows, _family_fn, min_sample=20,
                     global_default=10, floor_days=3)
    assert out["MOM"] == 10, (
        f"a slow-resolving family's calibration must cap at global_default "
        f"(10), got {out.get('MOM')} — this must never LOOSEN the clock")


def test_calibrate_never_undercuts_the_floor():
    """An extremely fast-resolving sample must not produce an unrealistically
    tight clock that trips on ordinary entry wobble."""
    from swing.signals.pace_calibration import _calibrate
    rows = [{"strategy": "CTL", "outcome_hold_days": 0} for _ in range(30)]
    out = _calibrate(rows, _family_fn, min_sample=20,
                     global_default=10, floor_days=3)
    assert out["CONTINUATION"] == 3, (
        f"calibration must not undercut floor_days (3), got "
        f"{out.get('CONTINUATION')}")


def test_calibrate_ignores_rows_with_no_usable_hold_days():
    from swing.signals.pace_calibration import _calibrate
    rows = ([{"strategy": "CTL", "outcome_hold_days": None} for _ in range(50)]
            + [{"strategy": "CTL", "outcome_hold_days": 5} for _ in range(25)])
    out = _calibrate(rows, _family_fn, min_sample=20,
                     global_default=10, floor_days=3)
    assert out.get("CONTINUATION") == 5, (
        f"rows with no hold_days must not be counted toward the sample or "
        f"the percentile, got {out.get('CONTINUATION')}")


# ── 2. Wired into evaluate_exit() ───────────────────────────────────────────

def _pos(strategy: str, entry=100.0, stop=94.0, hwm=None, **kw) -> dict:
    p = {"symbol": kw.pop("symbol", "X"), "entry_price": entry,
         "active_sl": stop, "planned_stop": stop,
         "high_water_mark": hwm if hwm is not None else entry - 1.0,
         "current_qty": 10, "actual_qty": 10, "framework": "SWING",
         "direction": "LONG", "strategy": strategy}
    p.update(kw)
    return p


def test_calibrated_clock_stalls_a_continuation_trade_the_flat_default_would_still_hold():
    """
    A CTL position, 7 sessions in, that never cleared 0.3R. Under the flat
    10-day default this must still HOLD. Under CONTINUATION's own
    calibrated 6-day clock (measured from 296 real resolved winners) it
    must have already been stalled out.
    """
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0   # risk 6.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos("CTL", entry, stop, hwm)
    with cfg_ctx({}):
        policy = load_exit_policy()

    policy["stall_days_by_family"] = {}
    d_uncalibrated = evaluate_exit(pos, hwm, 7, policy)
    assert d_uncalibrated["action"] != "EXIT_STALL", (
        "sanity check on the fixture: at the flat 10-day default, 7 "
        "sessions must NOT yet stall")

    policy["stall_days_by_family"] = {"CONTINUATION": 6}
    d_calibrated = evaluate_exit(pos, hwm, 7, policy)
    assert d_calibrated["action"] == "EXIT_STALL", (
        f"CONTINUATION's own calibrated 6-session clock must stall this "
        f"trade at session 7, got {d_calibrated['action']}: "
        f"{d_calibrated['detail']}")
    assert "calibrated clock is 6" in d_calibrated["detail"], (
        f"the detail string must name the calibrated clock, not just the "
        f"session count — got: {d_calibrated['detail']}")


def test_family_without_a_calibration_falls_back_to_the_flat_default():
    """RVS (or any family absent from stall_days_by_family) must behave
    exactly as it did before this session — the flat book-wide default."""
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos("RVS", entry, stop, hwm)
    with cfg_ctx({}):
        policy = load_exit_policy()
    policy["stall_days_by_family"] = {"CONTINUATION": 6, "MOM": 7}  # no RVS

    d7 = evaluate_exit(pos, hwm, 7, policy)
    assert d7["action"] != "EXIT_STALL", (
        "RVS has no calibration in this fixture — must still use the flat "
        "10-session default, not stall early")
    d10 = evaluate_exit(pos, hwm, 10, policy)
    assert d10["action"] == "EXIT_STALL", (
        "at session 10 the flat default must still fire, unchanged from "
        "pre-F-46 behaviour")


def test_missing_stall_days_by_family_key_does_not_break_existing_callers():
    """tools/replay/ladder.py and every existing test build a policy dict
    without this key. Must not raise, must behave exactly as before."""
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos("CTL", entry, stop, hwm)
    with cfg_ctx({}):
        policy = load_exit_policy()
    assert "stall_days_by_family" not in policy   # load_exit_policy() itself
                                                    # never sets it — only the
                                                    # daemon does
    d = evaluate_exit(pos, hwm, 10, policy)
    assert d["action"] == "EXIT_STALL", (
        "with no calibration key at all, the flat 10-session default must "
        "still fire exactly as before this session")


TESTS = [
    ("calibrate matches the measured CONTINUATION/MOM numbers",
     test_calibrate_matches_the_measured_continuation_and_mom_numbers),
    ("calibrate drops a family below the sample floor",
     test_calibrate_drops_a_family_below_the_sample_floor),
    ("calibrate never exceeds the global default",
     test_calibrate_never_exceeds_the_global_default),
    ("calibrate never undercuts the floor",
     test_calibrate_never_undercuts_the_floor),
    ("calibrate ignores rows with no usable hold_days",
     test_calibrate_ignores_rows_with_no_usable_hold_days),
    ("calibrated clock stalls a CONTINUATION trade the flat default would still hold",
     test_calibrated_clock_stalls_a_continuation_trade_the_flat_default_would_still_hold),
    ("family without a calibration falls back to the flat default",
     test_family_without_a_calibration_falls_back_to_the_flat_default),
    ("missing stall_days_by_family key does not break existing callers",
     test_missing_stall_days_by_family_key_does_not_break_existing_callers),
]
