"""
Track E, Stage E4 (docs/TRADEOS_ROADMAP.md) — structural break checked
from day one, and current sector-decay tightening.

Real-data anchor, checked live 24-Aug-2026: HINDCOPPER's own sector,
metals & mining, reads WEAKENING today in sector_strength (rank fell
7 -> 11 in 5 sessions, rank_delta_5d=-4) — a real, live signal
sector_rank_at_entry (checked only at the 3R runner decision, using the
frozen entry-day snapshot) cannot see at all during ordinary holding.
Both new rungs ship OFF by default (migration 095).
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


def _broken_sig() -> dict:
    """A signal context assess_trend() reads as BROKEN — enough negative
    checks to clear has_evidence and land on BROKEN, mirroring the real
    inputs assess_trend() actually reads."""
    return {
        "dist_sma50": -8.0, "rsi_daily": 28.0, "adx": 12.0,
        "vol_ratio": 0.4, "rs_vs_nifty": -3.0, "sector_rank_at_entry": 18,
    }


# ── 2b2. Early invalidation ─────────────────────────────────────────────────

def test_early_invalidation_shadow_only_by_default():
    """Off by default — a genuinely BROKEN reading below 1.0R must not
    change the ladder's own verdict."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)
    pos = _pos(entry, stop, ltp)
    policy = _policy()
    policy["_trend_ctx"] = {"X": _broken_sig()}
    # cfg_ctx({}) — the CODE's own default, not live system_config. See
    # test_stage_e3_ai_tighten_and_regime.py's identical note; broke the
    # same way the moment F-79's arming pass turned this switch on live.
    with cfg_ctx({}):
        d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] != "EXIT_INVALIDATED", (
        "swing_early_invalidation_enabled is off by default — must not "
        f"fire, got {d['action']}")


def test_early_invalidation_fires_when_armed_and_structure_is_broken():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)   # +0.2R — below the 1.0R gate
    pos = _pos(entry, stop, ltp)
    policy = _policy()
    policy["_trend_ctx"] = {"X": _broken_sig()}
    with cfg_ctx({"swing_early_invalidation_enabled": "true"}):
        d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] == "EXIT_INVALIDATED"
    assert d["reason"] == "THESIS_BROKEN_EARLY"


def test_early_invalidation_does_not_fire_without_broken_evidence():
    """An ordinary, unremarkable position at the same gain_r must not be
    cut just because it is armed — has_evidence + BROKEN is still
    required, same bar the profitable case already trusts."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)
    pos = _pos(entry, stop, ltp)
    policy = _policy()
    policy["_trend_ctx"] = {}   # no evidence at all
    with cfg_ctx({"swing_early_invalidation_enabled": "true"}):
        d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] != "EXIT_INVALIDATED"


def test_deterioration_check_existing_callers_unchanged():
    """The parametrization must not change ANY existing caller's
    behaviour — every existing call site passes none of the new args."""
    from control.exit_rules import assess_trend, deterioration_check, TrendQuality
    tq = TrendQuality(score=0.1, verdict="BROKEN", against=["a", "b"], checks=4)
    with cfg_ctx({"exit_deterioration_enabled": "true",
                 "exit_deterioration_min_r": "1.0"}):
        below = deterioration_check({}, 0.5, tq)   # below the default floor
        above = deterioration_check({}, 1.2, tq)   # above it
    assert below is None, "must still respect the default 1.0R floor"
    assert above is not None and above["action"] == "EXIT_DETERIORATION"
    assert above["reason"] == "THESIS_BROKEN"


# ── Sector-decay multiplier ──────────────────────────────────────────────────

def test_sector_decay_shadow_only_by_default():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["stall_days_by_family"] = {}
    # cfg_ctx({}) — same isolation note as the early-invalidation test
    # above. This one did not actually FAIL when the switch went live
    # (10*0.75 rounds to 8, one session past the session-7 check here —
    # a lucky timing coincidence, not real isolation) but was reading
    # the live value regardless; fixed for the same reason, not because
    # it was caught failing.
    with cfg_ctx({}):
        d7 = evaluate_exit(pos, hwm, 7, policy)
    assert d7["action"] != "EXIT_STALL", (
        "swing_sector_decay_enabled is off — WEAKENING must not shorten "
        "the stall clock while off")


def test_sector_decay_tightens_when_armed():
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["stall_days_by_family"] = {}   # flat default (10)
    with cfg_ctx({"swing_sector_decay_enabled": "true",
                 "swing_sector_decay_mult": "0.75"}):
        d8 = evaluate_exit(pos, hwm, 8, policy)
    # 10 * 0.75 = 7.5 -> rounds to 8 -> session 8 must now stall
    assert d8["action"] == "EXIT_STALL", (
        f"WEAKENING at x0.75 must shorten the 10-day default, got "
        f"{d8['action']} at session 8")


def test_sector_decay_does_not_loosen_a_leading_sector():
    """Tighten-only — a LEADING sector must not extend the clock, even
    though the regime multiplier is allowed to in a strong tape."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_sector_state"] = {"metals & mining": "LEADING"}
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_sector_decay_enabled": "true"}):
        d10 = evaluate_exit(pos, hwm, 10, policy)
    assert d10["action"] == "EXIT_STALL", (
        "a LEADING sector must apply x1.0 (no extra patience) — the flat "
        "10-day default still fires unchanged at session 10")


def test_sector_and_regime_multipliers_compose():
    """Both armed, both unfavourable: the multipliers must multiply, not
    override each other."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_current_regime"] = "RISK OFF"
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_regime_aware_exits_enabled": "true",
                 "swing_regime_mult_risk_off": "0.8",
                 "swing_sector_decay_enabled": "true",
                 "swing_sector_decay_mult": "0.8"}):
        d6 = evaluate_exit(pos, hwm, 6, policy)
    # 10 * 0.8 * 0.8 = 6.4 -> rounds to 6 -> session 6 must now stall
    assert d6["action"] == "EXIT_STALL", (
        f"composed 0.8 x 0.8 = 0.64 must shorten the 10-day default to "
        f"~6, got {d6['action']} at session 6")


# ── Sector-decay strength exemption — 24-Aug-2026, operator's own point ────
#
# "We should not be blocking the real candidates having the potential to
# move upwards e.g. with strong volumes." sector_state is a GROUP-level
# read; a genuine leader can outrun a lagging sector. These three prove
# the exemption fires ONLY on demonstrated individual strength, not on
# absence of data, and does not touch the confluence case (sector AND the
# stock's own volume both weak) where the tighten is still warranted.

def test_sector_decay_exempt_when_own_participation_strong():
    """Sector WEAKENING but this position's own vol_ratio is AT or ABOVE
    entry-day (>= the 1.0 floor) — the group-level read must not
    override demonstrated stock-level strength. Session 8 is BELOW the
    unmultiplied 10-day default: if the exemption works, mult stays x1.0
    and this must still HOLD; the un-exempted case (see the confluence
    and no-data tests below) shortens 10 -> 8 and DOES stall here, which
    is exactly the contrast this test has to prove."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["_participation_decay"] = {"X": 1.2}   # volume UP since entry
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_sector_decay_enabled": "true",
                 "swing_sector_decay_mult": "0.75"}):
        d8 = evaluate_exit(pos, hwm, 8, policy)
    assert d8["action"] != "EXIT_STALL", (
        f"exempted (own vol_ratio 1.2x >= 1.0 floor) must apply x1.0 — "
        f"the 10-day default must NOT have shortened, so session 8 must "
        f"still HOLD, got {d8['action']}")


def test_sector_decay_not_exempt_without_participation_data():
    """No participation data for this symbol (None, not a weak ratio) —
    absence of evidence is not evidence of strength; the exemption must
    require POSITIVE proof, not fire on a missing lookup. Existing
    behaviour (sector-decay applies) must be unchanged."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["stall_days_by_family"] = {}   # no _participation_decay key at all
    with cfg_ctx({"swing_sector_decay_enabled": "true",
                 "swing_sector_decay_mult": "0.75"}):
        d8 = evaluate_exit(pos, hwm, 8, policy)
    assert d8["action"] == "EXIT_STALL", (
        f"10 * 0.75 = 7.5 -> rounds to 8 -> must still stall at session 8 "
        f"when no participation data exists to justify an exemption, got "
        f"{d8['action']}")


def test_sector_decay_still_applies_on_confluence():
    """Sector WEAKENING AND this position's own participation has ALSO
    decayed — a real confluence, not a case for exemption."""
    from control.position_lifecycle import evaluate_exit
    entry, stop = 100.0, 94.0
    hwm = entry + 0.3 * (entry - stop)
    pos = _pos(entry, stop, hwm)
    policy = _policy()
    policy["_sector_state"] = {"metals & mining": "WEAKENING"}
    policy["_participation_decay"] = {"X": 0.3}   # also decayed
    policy["stall_days_by_family"] = {}
    with cfg_ctx({"swing_sector_decay_enabled": "true",
                 "swing_sector_decay_mult": "0.75"}):
        d8 = evaluate_exit(pos, hwm, 8, policy)
    assert d8["action"] == "EXIT_STALL", (
        f"confluence (sector weak AND own volume also weak) must still "
        f"tighten, got {d8['action']}")


TESTS = [
    ("early invalidation shadow-only by default",
     test_early_invalidation_shadow_only_by_default),
    ("early invalidation fires when armed and structure is broken",
     test_early_invalidation_fires_when_armed_and_structure_is_broken),
    ("early invalidation does not fire without broken evidence",
     test_early_invalidation_does_not_fire_without_broken_evidence),
    ("deterioration_check existing callers unchanged",
     test_deterioration_check_existing_callers_unchanged),
    ("sector decay shadow-only by default", test_sector_decay_shadow_only_by_default),
    ("sector decay tightens when armed", test_sector_decay_tightens_when_armed),
    ("sector decay does not loosen a leading sector",
     test_sector_decay_does_not_loosen_a_leading_sector),
    ("sector and regime multipliers compose",
     test_sector_and_regime_multipliers_compose),
    ("sector decay exempt when own participation strong",
     test_sector_decay_exempt_when_own_participation_strong),
    ("sector decay not exempt without participation data",
     test_sector_decay_not_exempt_without_participation_data),
    ("sector decay still applies on confluence",
     test_sector_decay_still_applies_on_confluence),
]
