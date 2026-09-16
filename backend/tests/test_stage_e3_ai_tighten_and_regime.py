"""
Track E, Stage E3 (docs/TRADEOS_ROADMAP.md) — ai_recommended_action
execution and the regime-aware exit-ladder multiplier.

HINDCOPPER's own 24-Aug-2026 ai_recommended_action was TIGHTEN_SL over a
live geopolitical risk ("Geopolitical Risk Escalation... could trigger a
sharper correction in the metal complex; protect gains with a tighter
stop") and nothing executed it — confirmed by grep, ai_decision_engine.py
writes it, alerts/send_alerts.py only displays it. Both new rungs ship OFF
by default (migration 094); these tests exercise both states.
"""

from __future__ import annotations

from tests import cfg_ctx


def _pos(entry=100.0, stop=94.0, hwm=None, **kw) -> dict:
    p = {"symbol": kw.pop("symbol", "X"), "entry_price": entry,
         "active_sl": stop, "planned_stop": stop,
         "high_water_mark": hwm if hwm is not None else entry,
         "current_qty": 10, "actual_qty": 10, "framework": "SWING",
         "direction": "LONG", "strategy": "CTL"}
    p.update(kw)
    return p


def _policy(**over) -> dict:
    from control.position_lifecycle import load_exit_policy
    with cfg_ctx({}):
        pol = load_exit_policy()
    pol.update(over)
    return pol


# ── 2c. AI TIGHTEN_SL ────────────────────────────────────────────────────────

def test_ai_tighten_shadow_only_by_default_does_not_change_the_action():
    """Off by default — the ladder's own verdict (HOLD here) must pass
    through unchanged, byte-identical to before this rung existed."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)   # +0.2R, ordinary HOLD territory
    pos = _pos(entry, stop, ltp, ai_recommended_action="TIGHTEN_SL",
              ai_action_reason="test risk")
    policy = _policy()
    # cfg_ctx({}) — NOT the live system_config. This asserts the CODE's
    # own default (cfg_bool(..., False)), not whatever the operator has
    # actually armed live. A version of this test without it silently
    # depended on system_config staying off, and broke the moment F-79's
    # arming pass turned it on for real — a live-state dependency
    # invisible until the live state actually changed.
    with cfg_ctx({}):
        d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] == "HOLD", (
        f"swing_ai_tighten_enabled is off by default — must not change "
        f"the ladder's own verdict, got {d['action']}")


def test_ai_tighten_fires_when_armed():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)
    pos = _pos(entry, stop, ltp, ai_recommended_action="TIGHTEN_SL",
              ai_action_reason="geopolitical risk")
    policy = _policy()
    with cfg_ctx({"swing_ai_tighten_enabled": "true",
                 "swing_ai_tighten_fraction": "0.5"}):
        d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] == "TRAIL_SL" and d["reason"] == "AI_TIGHTEN_SL"
    expected_sl = round(stop + 0.5 * (ltp - stop), 2)
    assert d["new_sl"] == expected_sl, (
        f"expected the stop to move halfway to price ({expected_sl}), "
        f"got {d['new_sl']}")
    assert d["new_sl"] > stop, "must never loosen the stop"


def test_ai_tighten_never_fires_without_the_recommendation():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)
    pos = _pos(entry, stop, ltp, ai_recommended_action="HOLD")
    policy = _policy()
    with cfg_ctx({"swing_ai_tighten_enabled": "true"}):
        d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] != "TRAIL_SL" or d.get("reason") != "AI_TIGHTEN_SL"


def test_ai_tighten_does_not_fire_for_trim_or_exit_recommendations():
    """Only TIGHTEN_SL is automated — TRIM/EXIT stay informational, per
    Stage E3's own explicit scope."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)
    policy = _policy()
    for action in ("TRIM", "EXIT", "NO_ACTION"):
        pos = _pos(entry, stop, ltp, ai_recommended_action=action)
        with cfg_ctx({"swing_ai_tighten_enabled": "true"}):
            d = evaluate_exit(pos, ltp, 3, policy)
        assert d.get("reason") != "AI_TIGHTEN_SL", (
            f"ai_recommended_action={action} must never auto-execute")


# ── Regime-aware multiplier ──────────────────────────────────────────────────

def test_regime_multiplier_is_a_noop_by_default():
    """Off by default — RISK OFF in policy must not change the stall
    clock at all when the switch is off."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_current_regime"] = "RISK OFF"
    policy["stall_days_by_family"] = {}   # flat default (10) applies
    # cfg_ctx({}) — see the identical note on the AI-tighten test above.
    with cfg_ctx({}):
        d7 = evaluate_exit(pos, hwm, 7, policy)
    assert d7["action"] != "EXIT_STALL", (
        "swing_regime_aware_exits_enabled is off — RISK OFF must not "
        "shorten the stall clock while the switch is off")


def test_regime_multiplier_tightens_the_stall_clock_when_armed():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_current_regime"] = "RISK OFF"
    policy["stall_days_by_family"] = {}   # flat default (10)
    with cfg_ctx({"swing_regime_aware_exits_enabled": "true",
                 "swing_regime_mult_risk_off": "0.7"}):
        d7 = evaluate_exit(pos, hwm, 7, policy)
    # 10 * 0.7 = 7 -> session 7 must now stall
    assert d7["action"] == "EXIT_STALL", (
        f"RISK OFF at x0.7 must shorten the 10-day default to 7, "
        f"got {d7['action']} at session 7")


def test_regime_multiplier_loosens_in_a_strong_tape_when_armed():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_current_regime"] = "RISK ON"
    policy["stall_days_by_family"] = {}   # flat default (10)
    with cfg_ctx({"swing_regime_aware_exits_enabled": "true",
                 "swing_regime_mult_risk_on": "1.2"}):
        d10 = evaluate_exit(pos, hwm, 10, policy)
    # 10 * 1.2 = 12 -> session 10 must NOT yet stall
    assert d10["action"] != "EXIT_STALL", (
        f"RISK ON at x1.2 must extend the 10-day default to 12, so "
        f"session 10 must still hold, got {d10['action']}")


def test_regime_multiplier_neutral_regime_is_unchanged():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_current_regime"] = "NEUTRAL"
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_regime_aware_exits_enabled": "true"}):
        d10 = evaluate_exit(pos, hwm, 10, policy)
    assert d10["action"] == "EXIT_STALL", (
        "NEUTRAL must apply a x1.0 multiplier — the flat 10-day default, "
        "unchanged, still fires at session 10")


def _run_cycles(pos, ltp, n, policy, flags):
    from control.position_lifecycle import evaluate_exit
    pos = dict(pos)
    with cfg_ctx(flags):
        for _ in range(n):
            d = evaluate_exit(pos, ltp, 1, policy)
            if d["action"] == "TRAIL_SL" and d.get("new_sl"):
                pos["active_sl"] = d["new_sl"]
                pos["trail_activated"] = True
            elif d["action"].startswith("EXIT"):
                return pos, d
    return pos, None


def test_ai_tighten_does_not_ratchet_onto_price_across_cycles():
    """The daemon re-evaluates every 15s. Moving sl halfway to price on each
    call converged the stop onto the price within ~3 minutes and closed all 13
    AI-tightened swing trades on their first downtick (26-Aug to 16-Sep-2026).
    Sixty cycles at an unchanged price must leave the stop where one left it."""
    entry, stop = 1902.90, 1759.40
    ltp = 1895.0
    pos = _pos(entry, stop, entry, ai_recommended_action="TIGHTEN_SL",
               ai_action_reason="geopolitical")
    flags = {"swing_ai_tighten_enabled": "true", "swing_ai_tighten_fraction": "0.5"}
    one, _ = _run_cycles(pos, ltp, 1, _policy(), flags)
    many, exited = _run_cycles(pos, ltp, 60, _policy(), flags)
    assert exited is None, f"60 cycles at a flat price closed the trade: {exited}"
    assert many["active_sl"] == one["active_sl"], (
        f"stop kept moving at an unchanged price: {one['active_sl']} after one "
        f"cycle, {many['active_sl']} after sixty")
    gap_r = (ltp - many["active_sl"]) / (entry - stop)
    assert gap_r >= 0.4, f"stop sits {gap_r:.2f}R under price — a downtick exit, not a tighten"


def test_ai_tighten_still_follows_a_rising_price():
    """Idempotent at one price, but a higher price may still lift the stop."""
    entry, stop = 100.0, 94.0
    pos = _pos(entry, stop, 104.0, ai_recommended_action="TIGHTEN_SL")
    flags = {"swing_ai_tighten_enabled": "true", "swing_ai_tighten_fraction": "0.5"}
    a, _ = _run_cycles(pos, 101.0, 5, _policy(), flags)
    b, _ = _run_cycles(a, 103.0, 5, _policy(), flags)
    assert b["active_sl"] > a["active_sl"] > stop


TESTS = [
    ("ai tighten does not ratchet onto price across 15s cycles",
     test_ai_tighten_does_not_ratchet_onto_price_across_cycles),
    ("ai tighten still follows a rising price", test_ai_tighten_still_follows_a_rising_price),
    ("ai tighten shadow-only by default does not change the action",
     test_ai_tighten_shadow_only_by_default_does_not_change_the_action),
    ("ai tighten fires when armed", test_ai_tighten_fires_when_armed),
    ("ai tighten never fires without the recommendation",
     test_ai_tighten_never_fires_without_the_recommendation),
    ("ai tighten does not fire for TRIM/EXIT recommendations",
     test_ai_tighten_does_not_fire_for_trim_or_exit_recommendations),
    ("regime multiplier is a no-op by default",
     test_regime_multiplier_is_a_noop_by_default),
    ("regime multiplier tightens the stall clock when armed",
     test_regime_multiplier_tightens_the_stall_clock_when_armed),
    ("regime multiplier loosens in a strong tape when armed",
     test_regime_multiplier_loosens_in_a_strong_tape_when_armed),
    ("regime multiplier neutral regime is unchanged",
     test_regime_multiplier_neutral_regime_is_unchanged),
]
