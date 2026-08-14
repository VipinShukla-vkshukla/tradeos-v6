"""
The swing planner's two defects: a regime knob that moves R, and no cost model.

WHAT THIS CATCHES
------------------
`analysis/risk_model.py` scaled the STOP by `regime_k` (`:161`) and left the
TARGET unscaled (`:188`). Both are ATR distances and they are the two sides of
one ratio, so scaling one of them made planned R a function of the regime:

    planned R = target_atr_mult / (stop_atr_mult * regime_k) = 3.0 / (1.5 * k)

    TRENDING  k=0.95 -> 2.1050
    RISK ON   k=1.00 -> 2.0000
    NEUTRAL   k=1.05 -> 1.9048   <- every session on record
    RECOVERING k=1.15 -> 1.7391
    RISK OFF  k=1.25 -> 1.6000

R therefore SHRINKS as conditions worsen — more risk per share for identical
reward, exactly when the market is least willing to pay for it. The design
point of 2R is reachable only in RISK ON, a regime this book has never traded.

The second defect: the module imported `dataclasses` and nothing else. It set a
target with no knowledge of the friction that target has to clear. A 1.9048R
plan on a Rs 4,000 CNC clip with a 3.15% stop carries 0.19R of statutory
friction and needs 1.975R to break even at the design hit rate — it was planned
0.07R short and nothing in the planner could see it.

WHAT IS PINNED HERE
-------------------
Both fixes ship INERT (`risk_regime_scales_target` and
`risk_min_planned_r_enabled` both default off), so these tests assert the
CURRENT behaviour under `cfg_ctx({})` and the FIXED behaviour only with the
switch on. Arming either one is a separate decision.

The two that matter most are the mirror pair CLAUDE.md keeps demanding:
`test_floor_rejects_a_plan_that_cannot_clear_its_own_friction` proves the floor
CAN FAIL, and `test_floor_admits_a_realistic_plan_once_regime_symmetry_is_on`
proves it CAN PASS — on the same setup, which is what makes the pair evidence
rather than two unrelated assertions.
"""

from __future__ import annotations

from tests import cfg_ctx, approx


# Rs 20,000 — the real account. This laptop's TOTAL_CAPITAL env reads Rs 30,000
# (FINDINGS F-16), and `capital_for("SWING")` would hand the planner that, so
# every test that touches sizing pins capital explicitly rather than inheriting
# whichever machine it runs on.
CAP = {"risk_plan_capital": "20000"}

REGIME_RR = {
    "TRENDING":   2.105,
    "RISK ON":    2.000,
    "NEUTRAL":    1.905,
    "RECOVERING": 1.739,
    "RISK OFF":   1.600,
}


def _levels(regime="NEUTRAL", entry=500.0, atr=15.0, structure=None):
    from analysis.risk_model import compute_trade_levels
    return compute_trade_levels(entry, atr, anchor_price=entry,
                                structure_stop=structure, regime=regime)


# ── 1. the defect, pinned as it stands today ────────────────────────────────

def test_default_reproduces_the_1_9048_constant():
    """Regression pin. If this moves, the shipped-inert promise was broken."""
    with cfg_ctx(CAP):
        lv = _levels("NEUTRAL")
    assert lv.valid
    assert lv.rr == 1.905, f"NEUTRAL planned R is {lv.rr}, not the 1.9048 constant"


def test_default_leaves_the_target_unscaled_across_every_regime():
    with cfg_ctx(CAP):
        targets = {r: _levels(r).target for r in REGIME_RR}
    assert len(set(targets.values())) == 1, (
        f"the target moved with regime while the switch is off: {targets}")


def test_default_regime_sweep_matches_the_documented_ladder():
    with cfg_ctx(CAP):
        got = {r: _levels(r).rr for r in REGIME_RR}
    assert got == REGIME_RR, f"regime->R ladder changed: {got}"


# ── 2. the fix: regime must not change reward per unit risk ─────────────────

def test_symmetry_on_makes_planned_r_regime_invariant():
    """
    The whole point. With the target scaled by the same k as the stop, planned
    R collapses to target_atr_mult / stop_atr_mult = 2.0 in EVERY regime.
    """
    with cfg_ctx({**CAP, "risk_regime_scales_target": "1"}):
        got = {r: _levels(r).rr for r in REGIME_RR}
    assert set(got.values()) == {2.0}, (
        f"planned R still depends on the regime after the fix: {got}")


def test_symmetry_on_widens_the_target_in_a_stressed_regime():
    """Direction check — RISK OFF must widen BOTH sides, not just the stop."""
    with cfg_ctx(CAP):
        before = _levels("RISK OFF")
    with cfg_ctx({**CAP, "risk_regime_scales_target": "1"}):
        after = _levels("RISK OFF")
    assert after.stop == before.stop, "the stop must not move — only the target"
    assert after.target > before.target
    assert after.rr > before.rr


def test_symmetry_leaves_a_structure_stop_plan_alone():
    """
    A structural stop is a PRICE, not an ATR distance, so `regime_k` never
    touched its risk. Scaling the target there would raise R with no offsetting
    change in risk — a 5% uplift on 310 of 995 plans for free. The k is applied
    to the target only when it was applied to the stop.
    """
    struct = 490.0            # tighter than the 476.375 ATR stop, outside noise
    with cfg_ctx(CAP):
        before = _levels("NEUTRAL", structure=struct)
    with cfg_ctx({**CAP, "risk_regime_scales_target": "1"}):
        after = _levels("NEUTRAL", structure=struct)
    assert before.stop_source == "structure", "fixture no longer takes the structural stop"
    assert after.stop_source == "structure"
    assert after.target == before.target, (
        f"target moved from {before.target} to {after.target} on a structure "
        f"stop, whose risk regime_k never scaled")
    assert after.rr == before.rr


def test_regime_k_is_reported_so_the_scaling_is_auditable():
    with cfg_ctx(CAP):
        assert _levels("RISK OFF").regime_k == 1.25
        assert _levels("TRENDING").regime_k == 0.95


# ── 3. the cost model — LEDGER basis, and the plan's own clip ───────────────

def test_friction_uses_the_ledger_basis_not_the_gate_basis():
    """
    `entry_leg + exit_leg` (statutory only), NOT `round_trip` (which adds 5 bps
    of slippage per leg). The two differ by a constant +0.100pp of position,
    which on CNC is a 1.10-1.17x adjustment. Planned R is compared against
    REALISED R, and the realised side — `tools/expectancy_ledger.py` — prices
    friction statutorily because slippage is already inside the fill price.
    """
    from intraday.cost_model import entry_leg, exit_leg, round_trip
    with cfg_ctx(CAP):
        lv = _levels("NEUTRAL")
        qty = lv.clip_qty
        ledger = entry_leg(lv.entry, qty, product="CNC") + exit_leg(lv.entry, qty, product="CNC")
        gate = round_trip(lv.entry, qty, product="CNC").total

    assert qty > 0
    assert lv.cost_basis == "ledger"
    assert approx(lv.friction_r, round(ledger / (qty * lv.risk_per_share), 4), 1e-12), (
        f"friction_r {lv.friction_r} is not (entry_leg+exit_leg)/risk_rupees")
    assert gate > ledger, "fixture broken — the gate basis must be the dearer one"
    assert not approx(lv.friction_r, gate / (qty * lv.risk_per_share), 1e-6), (
        "friction_r matches the GATE basis; the planner picked the wrong one")


def test_clip_reproduces_the_production_sizing_rule():
    """
    min(risk budget / risk-per-share, max_position_pct of capital / price) —
    the same two terms `portfolio_constraints.py:220-226` applies with an empty
    book. Rs 20,000 at 1% risk = Rs 200; 20% max position = Rs 4,000.
    """
    with cfg_ctx(CAP):
        lv = _levels("NEUTRAL")          # entry 500, risk/share 23.625
    assert lv.clip_qty == min(int(200 // 23.625), int(4000 // 500)) == 8
    assert lv.clip_value == 8 * 500.0


def test_required_rr_is_the_breakeven_identity_at_the_design_hit_rate():
    """
    required = (1 - h + friction) / h, the same identity unit_economics uses —
    and it must reconcile through the two REPORTED numbers, not through an
    unrounded intermediate a reader cannot see.
    """
    with cfg_ctx({**CAP, "risk_plan_hit_rate": "0.40"}):
        lv = _levels("NEUTRAL")
    assert approx(lv.required_rr, round((1 - 0.40 + lv.friction_r) / 0.40, 3), 1e-12), (
        f"friction {lv.friction_r} and required {lv.required_rr} do not "
        f"reconcile through the break-even identity")


def test_a_lower_hit_rate_demands_a_higher_planned_r():
    with cfg_ctx({**CAP, "risk_plan_hit_rate": "0.40"}):
        at40 = _levels("NEUTRAL").required_rr
    with cfg_ctx({**CAP, "risk_plan_hit_rate": "0.30"}):
        at30 = _levels("NEUTRAL").required_rr
    assert at30 > at40


# ── 4. the floor — it must be able to FAIL, and to PASS ─────────────────────

# entry 500, ATR 2% = Rs 10 -> NEUTRAL stop 484.25, risk 15.75 (3.15%), clip 8
# shares / Rs 4,000. CNC statutory on that clip is ~Rs 23.94 = 0.19R, so
# break-even at h=0.40 wants 1.975R. The plan is built at 1.9048R.
THIN = dict(entry=500.0, atr=10.0)


def test_floor_is_off_by_default_and_the_thin_plan_survives():
    with cfg_ctx(CAP):
        lv = _levels("NEUTRAL", **THIN)
    assert lv.valid, "the floor fired while shipped inert"
    assert lv.friction_r > 0, "friction must still be REPORTED when the gate is off"
    assert lv.rr < lv.required_rr, (
        "fixture broken — this plan is supposed to sit below its own break-even")


def test_floor_rejects_a_plan_that_cannot_clear_its_own_friction():
    """The check CAN FAIL."""
    with cfg_ctx({**CAP, "risk_min_planned_r_enabled": "1"}):
        lv = _levels("NEUTRAL", **THIN)
    assert not lv.valid
    assert lv.reject_reason.startswith("below_min_planned_r"), lv.reject_reason
    assert "1.905" in lv.reject_reason and "1.97" in lv.reject_reason, (
        f"the reason must name both numbers, got {lv.reject_reason!r}")


def test_floor_admits_a_realistic_plan_once_regime_symmetry_is_on():
    """
    The check CAN PASS — CLAUDE.md's mirror rule. Same setup, same floor; the
    regime fix alone lifts it from 1.9048R to 2.0R, over its own 1.975R bar.
    """
    with cfg_ctx({**CAP, "risk_min_planned_r_enabled": "1",
                  "risk_regime_scales_target": "1"}):
        lv = _levels("NEUTRAL", **THIN)
    assert lv.valid, f"a 2.0R plan was refused: {lv.reject_reason}"
    assert lv.rr == 2.0
    assert lv.rr > lv.required_rr


def test_floor_admits_an_ordinary_wide_stop_plan_even_unfixed():
    """
    The floor must not be a wall. The 3%-ATR case — the module's own worked
    example — carries less friction per R because the stop is wider, and clears
    1.9048R with the regime fix still off.
    """
    with cfg_ctx({**CAP, "risk_min_planned_r_enabled": "1"}):
        lv = _levels("NEUTRAL")
    assert lv.valid, f"refused an ordinary plan: {lv.reject_reason}"


def test_margin_above_breakeven_is_configurable_and_bites():
    with cfg_ctx({**CAP, "risk_min_planned_r_enabled": "1",
                  "risk_plan_r_margin": "0.50"}):
        lv = _levels("NEUTRAL")
    assert not lv.valid, "a 0.50R margin should refuse a 1.9048R plan"


# ── 5. permissive where it has no opinion ───────────────────────────────────

def test_an_unfundable_plan_is_not_refused_by_the_floor():
    """
    "A cold start must be PERMISSIVE." A share the account cannot buy one of
    has no clip, so it has no friction — that is ABSENT evidence, not bad
    evidence. Refusing to fund it is `portfolio_constraints`' job, and it says
    so in its own reason string. The planner must not pre-empt that with a
    cost verdict it cannot compute.
    """
    with cfg_ctx({**CAP, "risk_min_planned_r_enabled": "1"}):
        lv = _levels("NEUTRAL", entry=45000.0, atr=1350.0)   # > the Rs 4,000 ceiling
    assert lv.clip_qty == 0
    assert lv.valid, f"refused an unfundable plan on cost: {lv.reject_reason}"
    assert lv.cost_basis == "unfunded"
    assert lv.friction_r == 0.0 and lv.required_rr == 0.0


def test_a_broken_cost_model_never_refuses():
    """
    Same rule one layer down: if the charge schedule cannot be read at all, the
    planner has no opinion and must say so rather than reject. Mirrors
    `portfolio_constraints.py:295` — "never refuse on a broken cost model".
    """
    import analysis.risk_model as rm
    saved = rm._statutory_round_trip

    def boom(*a, **k):
        raise RuntimeError("no config")

    rm._statutory_round_trip = boom
    try:
        with cfg_ctx({**CAP, "risk_min_planned_r_enabled": "1"}):
            lv = _levels("NEUTRAL", **THIN)
    finally:
        rm._statutory_round_trip = saved
    assert lv.valid, f"refused on a broken cost model: {lv.reject_reason}"
    assert lv.cost_basis == "unavailable"


def test_the_planner_actually_imports_a_cost_model():
    """
    The Stage 2c finding was that every cost token was absent from this module.
    Grep the source, not the behaviour — a future refactor that drops the
    import would otherwise only show up as friction silently reading 0.0.
    """
    from pathlib import Path
    import analysis.risk_model as rm
    src = Path(rm.__file__).read_text(encoding="utf-8")
    assert "entry_leg" in src and "exit_leg" in src, (
        "risk_model.py no longer references the statutory cost legs")


TESTS = [
    ("default reproduces the 1.9048 constant",      test_default_reproduces_the_1_9048_constant),
    ("default leaves the target unscaled",          test_default_leaves_the_target_unscaled_across_every_regime),
    ("default regime ladder unchanged",             test_default_regime_sweep_matches_the_documented_ladder),
    ("symmetry makes planned R regime-invariant",   test_symmetry_on_makes_planned_r_regime_invariant),
    ("symmetry widens the target in RISK OFF",      test_symmetry_on_widens_the_target_in_a_stressed_regime),
    ("symmetry leaves a structure stop alone",      test_symmetry_leaves_a_structure_stop_plan_alone),
    ("regime_k is reported",                        test_regime_k_is_reported_so_the_scaling_is_auditable),
    ("friction uses the LEDGER basis",              test_friction_uses_the_ledger_basis_not_the_gate_basis),
    ("clip reproduces the sizing rule",             test_clip_reproduces_the_production_sizing_rule),
    ("required R is the break-even identity",       test_required_rr_is_the_breakeven_identity_at_the_design_hit_rate),
    ("a lower hit rate demands a higher R",         test_a_lower_hit_rate_demands_a_higher_planned_r),
    ("floor off by default",                        test_floor_is_off_by_default_and_the_thin_plan_survives),
    ("floor CAN FAIL",                              test_floor_rejects_a_plan_that_cannot_clear_its_own_friction),
    ("floor CAN PASS once symmetry is on",          test_floor_admits_a_realistic_plan_once_regime_symmetry_is_on),
    ("floor admits an ordinary plan unfixed",       test_floor_admits_an_ordinary_wide_stop_plan_even_unfixed),
    ("margin above break-even bites",               test_margin_above_breakeven_is_configurable_and_bites),
    ("unfundable plan is not refused on cost",      test_an_unfundable_plan_is_not_refused_by_the_floor),
    ("broken cost model never refuses",             test_a_broken_cost_model_never_refuses),
    ("the planner imports a cost model",            test_the_planner_actually_imports_a_cost_model),
]
