"""
Track E, Stage E4 (docs/TRADEOS_ROADMAP.md) — participation/delivery decay,
the swing-cadence version of the intraday F-45 volume-decay idea.

vol_ratio on the entry-day session vs. the latest available session, per
held SWING symbol — the daemon and tools.simulate both fetch this once per
policy load via control.position_lifecycle::load_live_exit_context and
surface it as policy["_participation_decay"] (a {symbol: ratio} dict), the
same shape `_sector_state` already uses. Ships OFF by default (migration
110).

The giveback guard, not the stall clock, is used to isolate the min-
sessions-held gate below: the stall clock's own 10-day default makes any
boundary test at sessions_held=1 vs 2 vacuous (both are far short of 10
either way), while the giveback guard fires at ANY sessions_held once its
own peak/kept-fraction condition is met — so it is sensitive to the
multiplier at exactly the session count under test.
"""

from __future__ import annotations

from tests import cfg_ctx


def _pos(entry=100.0, stop=94.0, hwm=None, **kw) -> dict:
    p = {"symbol": kw.pop("symbol", "X"), "entry_price": entry,
         "active_sl": stop, "planned_stop": stop,
         "high_water_mark": hwm if hwm is not None else entry,
         "current_qty": 10, "actual_qty": 10, "framework": "SWING",
         "direction": "LONG", "strategy": "CTL", "sector": "metals & mining"}
    p.update(kw)
    return p


def _policy(**over) -> dict:
    from control.position_lifecycle import load_exit_policy
    with cfg_ctx({}):
        pol = load_exit_policy()
    pol.update(over)
    return pol


# entry=100, stop=94 -> risk=6R. Peak at 0.7R (below giveback_runner_min_r
# =1.0, so the flat giveback_pct=50% tier applies, not the 30% runner
# tier). Held back to 0.4R:
#   mult=1.0  -> gb_pct=50  -> fires below kept<0.50 -> kept=0.4/0.7=0.571
#                              does NOT trigger
#   mult=0.75 -> gb_pct=37.5 -> fires below kept<0.625 -> 0.571 DOES trigger
# chosen so the SAME fixture proves both "off/ungated -> HOLD" and
# "armed+decayed+min-sessions-met -> EXIT_GIVEBACK" with one number.
def _giveback_fixture(sessions_held: int):
    entry, stop = 100.0, 94.0
    hwm = entry + 0.7 * (entry - stop)     # peaked at 0.7R
    ltp = entry + 0.4 * (entry - stop)     # now back to 0.4R
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_participation_decay"] = {"X": 0.3}   # well below 0.5 threshold
    policy["stall_days_by_family"] = {}
    return pos, ltp, sessions_held, policy


def test_participation_decay_shadow_only_by_default():
    from control.position_lifecycle import evaluate_exit
    pos, ltp, held, policy = _giveback_fixture(sessions_held=2)
    d = evaluate_exit(pos, ltp, held, policy)
    assert d["action"] != "EXIT_GIVEBACK", (
        "swing_participation_decay_enabled is off by default — a decayed "
        f"vol_ratio must not tighten the giveback guard, got {d['action']}")


def test_participation_decay_tightens_when_armed():
    from control.position_lifecycle import evaluate_exit
    pos, ltp, held, policy = _giveback_fixture(sessions_held=2)
    with cfg_ctx({"swing_participation_decay_enabled": "true",
                 "swing_participation_decay_threshold": "0.5",
                 "swing_participation_decay_mult": "0.75"}):
        d = evaluate_exit(pos, ltp, held, policy)
    assert d["action"] == "EXIT_GIVEBACK", (
        f"decayed vol_ratio (0.3 < 0.5 threshold) at x0.75 must tighten "
        f"the giveback guard from 50% to 37.5%, kept=0.571 must now clear "
        f"it — got {d['action']}")


def test_participation_decay_does_not_fire_before_min_sessions_held():
    """The multiplier must stay 1.0 on day one/two of a fresh position —
    never flag on entry day itself, matching the same reasoning
    early-invalidation and sector-decay already apply at their own
    gates."""
    from control.position_lifecycle import evaluate_exit
    pos, ltp, _held, policy = _giveback_fixture(sessions_held=1)
    with cfg_ctx({"swing_participation_decay_enabled": "true",
                 "swing_participation_decay_threshold": "0.5",
                 "swing_participation_decay_mult": "0.75"}):
        d = evaluate_exit(pos, ltp, 1, policy)
    assert d["action"] != "EXIT_GIVEBACK", (
        "sessions_held=1 is below the 2-session floor — must not apply "
        f"the participation multiplier yet, got {d['action']}")


def test_participation_decay_does_not_fire_above_threshold():
    """Tighten-only, and only when actually decayed — a vol_ratio still
    near or above its entry-day value must apply x1.0."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.7 * (entry - stop)
    ltp = entry + 0.4 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_participation_decay"] = {"X": 0.8}   # above the 0.5 threshold
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_participation_decay_enabled": "true",
                 "swing_participation_decay_threshold": "0.5",
                 "swing_participation_decay_mult": "0.75"}):
        d = evaluate_exit(pos, ltp, 2, policy)
    assert d["action"] != "EXIT_GIVEBACK", (
        f"vol_ratio 0.8 is above the 0.5 decay threshold — must not "
        f"tighten, got {d['action']}")


def test_participation_regime_and_sector_multipliers_compose():
    """All three armed and all three unfavourable: the multipliers must
    multiply, not override each other — same proof the Stage E4 sector/
    regime pair already carries, extended to three factors."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_current_regime"] = "RISK OFF"
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["_participation_decay"] = {"X": 0.3}
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_regime_aware_exits_enabled": "true",
                 "swing_regime_mult_risk_off": "0.9",
                 "swing_sector_decay_enabled": "true",
                 "swing_sector_decay_mult": "0.9",
                 "swing_participation_decay_enabled": "true",
                 "swing_participation_decay_threshold": "0.5",
                 "swing_participation_decay_mult": "0.9"}):
        d = evaluate_exit(pos, hwm, 8, policy)
    # 10 * 0.9 * 0.9 * 0.9 = 7.29 -> rounds to 7 -> session 8 must stall
    assert d["action"] == "EXIT_STALL", (
        f"composed 0.9^3 = 0.729 must shorten the 10-day default to ~7, "
        f"got {d['action']} at session 8")


TESTS = [
    ("participation decay shadow-only by default",
     test_participation_decay_shadow_only_by_default),
    ("participation decay tightens when armed",
     test_participation_decay_tightens_when_armed),
    ("participation decay does not fire before min sessions held",
     test_participation_decay_does_not_fire_before_min_sessions_held),
    ("participation decay does not fire above threshold",
     test_participation_decay_does_not_fire_above_threshold),
    ("participation, regime and sector multipliers compose",
     test_participation_regime_and_sector_multipliers_compose),
]
