"""
F-43, 20-Aug-2026 — full swing-book review against the last 15 closed trades.

Four checks that did not exist before this session, each anchored to a real
number from docs/FINDINGS.md's F-43 entry:

  1. `_rank_floor_blocks` — TRAVELFOOD entered 14-Aug at composite rank -16
     because act_on_candidates() sends anything outside the top-N contenders
     straight to the order path with no rank check at all.
  2. `regime_min_rr` — the live entry gate used a flat 1.0R min_rr regardless
     of what the tape was doing; the evening pipeline's own regime-scaled
     thresholds were computed and never read.
  3. The repriced partial rung — median peak run across 13 closed swing
     trades was 0.67R and only 2 of 13 ever cleared 1.0R, so a 1.5R partial
     rung was very close to unreachable.
  4. The tiered give-back guard — 7 of the last 9 winning swing exits gave
     back within 1-6% of exactly 50% of their peak, because the flat 50%
     setting was the only rule any of them ever touched.

`entry_respect_filter_reason` (the GABRIEL-shaped refusal) already has full
coverage in test_gabriel_gap.py — the code was correct, only the config row
was missing. Nothing new to test there.
"""

from __future__ import annotations

from tests import cfg_ctx


# ── 1. Absolute rank floor ──────────────────────────────────────────────────

def test_rank_floor_blocks_travelfood_shaped_negative_rank():
    from intraday.engine import _rank_floor_blocks
    assert _rank_floor_blocks(here_total=-16.0, floor=0.0), (
        "a plan ranked -16 — sub-1.0 R:R, AI-flagged risk, an event inside "
        "2 days — must be refused by the absolute floor regardless of "
        "whether it was ever compared against today's field")


def test_rank_floor_admits_an_ordinary_positive_rank():
    from intraday.engine import _rank_floor_blocks
    assert not _rank_floor_blocks(here_total=42.0, floor=0.0), \
        "an ordinary positive-rank plan must not be caught by the floor"


def test_rank_floor_is_independent_of_the_relative_gate():
    """
    The two gates ask different questions and must not be conflatable into
    one. A plan can clear the relative top-N gate (it is in today's best
    field) while still being, on its own terms, a bad trade — a thin field
    of six candidates where the "best" one is still rank -3.
    """
    from intraday.engine import _legacy_rank_gate_blocks, _rank_floor_blocks
    field = [-3.0, -10.0, -12.0, -20.0, -25.0, -30.0]
    relative_blocked = _legacy_rank_gate_blocks(
        here_total=-3.0, field_totals=field, keep=6, alloc_live_swing=False)
    absolute_blocked = _rank_floor_blocks(here_total=-3.0, floor=0.0)
    assert not relative_blocked, "rank -3 IS the best of this thin field"
    assert absolute_blocked, (
        "but -3 is still below zero on its own terms and must be refused — "
        "this is exactly the gap the relative gate cannot see")


# ── 2. Regime-aware min_rr ───────────────────────────────────────────────────

def test_regime_min_rr_reads_the_regime_specific_override():
    from analysis.trade_decision import regime_min_rr
    cfg = {"min_rr_to_enter": "1.0",
           "min_rr_to_enter_RISK_OFF": "1.5",
           "min_rr_to_enter_NEUTRAL": "1.0"}
    with cfg_ctx(cfg):
        assert regime_min_rr("RISK OFF") == 1.5, (
            "a deteriorating tape must raise the bar above the flat "
            "default — this is the exact number the pipeline's own gate "
            "already used while the live daemon ignored it")
        assert regime_min_rr("NEUTRAL") == 1.0


def test_regime_min_rr_falls_back_to_the_flat_default_for_an_unknown_regime():
    from analysis.trade_decision import regime_min_rr
    with cfg_ctx({"min_rr_to_enter": "1.0"}):
        assert regime_min_rr("SOME_FUTURE_REGIME") == 1.0
        assert regime_min_rr(None) == 1.0, \
            "a missing regime must not raise an exception or refuse everything"


def test_regime_min_rr_key_convention_matches_space_separated_regime():
    """
    The swing regime is written space-separated ("RISK ON") — docs/
    TERMINOLOGY.md — while system_config keys cannot contain spaces. Must
    transform the same way dynamic_registry._build_regime_rr_keys already
    does, or every space-separated regime silently falls through to the
    flat default with no error.
    """
    from analysis.trade_decision import regime_min_rr
    with cfg_ctx({"min_rr_to_enter": "1.0", "min_rr_to_enter_RISK_ON": "1.1"}):
        assert regime_min_rr("RISK ON") == 1.1, (
            "regime_min_rr('RISK ON') must find min_rr_to_enter_RISK_ON — "
            "got the flat default, meaning the space was never converted "
            "to the underscore the config key actually uses")


# ── 3. Repriced partial rung ─────────────────────────────────────────────────

def test_partial_fires_at_the_repriced_one_r_not_the_old_1_5r():
    """
    HINDCOPPER-shaped: entry 523.65, stop 484.76 (risk 38.89), currently at
    1.004R. Under the OLD 1.5R rung this holds all the way to +9%; under the
    repriced 1.0R rung it must book half now.
    """
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 523.65, 484.76
    ltp = entry + 1.02 * (entry - stop)   # 1.02R
    pos = {"symbol": "HINDCOPPER", "entry_price": entry, "planned_stop": stop,
           "active_sl": stop, "high_water_mark": ltp, "current_qty": 8,
           "actual_qty": 8, "framework": "SWING", "direction": "LONG"}
    with cfg_ctx({}):
        policy = load_exit_policy()
    d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] == "BOOK_PARTIAL", (
        f"at 1.02R the repriced ladder must book a partial, got "
        f"{d['action']}: {d['detail']}")
    assert policy["partial_book_r"] == 1.0, (
        f"exit_partial_book_r must default to 1.0 post-reprice, got "
        f"{policy['partial_book_r']} — the old 1.5R default was reachable "
        f"on only 2 of 13 measured closed swing trades")


def test_breakeven_engages_at_the_repriced_half_r():
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0   # risk 6.0
    ltp = entry + 0.55 * (entry - stop)   # 0.55R — below the old 1.0R rung
    pos = {"symbol": "X", "entry_price": entry, "planned_stop": stop,
           "active_sl": stop, "high_water_mark": ltp, "current_qty": 10,
           "actual_qty": 10, "framework": "SWING", "direction": "LONG",
           "partial_booked_qty": 1}   # already booked, so BOOK_PARTIAL can't refire
    with cfg_ctx({}):
        policy = load_exit_policy()
    d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] == "TRAIL_SL" and d["reason"] == "BREAKEVEN", (
        f"at 0.55R the repriced 0.5R breakeven rung must have fired, got "
        f"{d['action']}/{d.get('reason')}: {d['detail']}")


# ── 4. Tiered give-back guard ─────────────────────────────────────────────────

def test_giveback_stays_loose_below_the_runner_line():
    """
    Peak 0.8R (below giveback_runner_min_r=1.0), given back to 0.42R — kept
    52.5%, inside the loose 50% allowance's complement... i.e. gave back
    47.5%, UNDER the 50% limit, so this must still HOLD.
    """
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0
    hwm = entry + 0.8 * (entry - stop)
    ltp = entry + 0.42 * (entry - stop)
    pos = {"symbol": "X", "entry_price": entry, "planned_stop": stop,
           "active_sl": stop, "high_water_mark": hwm, "current_qty": 10,
           "actual_qty": 10, "framework": "SWING", "direction": "LONG"}
    with cfg_ctx({}):
        policy = load_exit_policy()
    d = evaluate_exit(pos, ltp, 3, policy)
    assert d["action"] != "EXIT_GIVEBACK", (
        f"below the runner line the guard must stay at the loose 50% "
        f"allowance — got {d['action']}: {d['detail']}")


def test_giveback_tightens_once_a_partial_should_be_banked():
    """
    THE CASE THIS FIX EXISTS FOR. Peak 1.4R (above giveback_runner_min_r),
    given back to 0.9R — kept 64%, gave back 36%. Under the OLD flat 50%
    limit this holds (36% < 50%). Under the new tiered 30% limit for a
    position past the runner line, 36% > 30% and it must exit.
    """
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0
    hwm = entry + 1.4 * (entry - stop)
    ltp = entry + 0.9 * (entry - stop)
    pos = {"symbol": "X", "entry_price": entry, "planned_stop": stop,
           "active_sl": stop, "high_water_mark": hwm, "current_qty": 10,
           "actual_qty": 10, "framework": "SWING", "direction": "LONG",
           "partial_booked_qty": 5}
    with cfg_ctx({}):
        policy = load_exit_policy()
    d = evaluate_exit(pos, ltp, 5, policy)
    assert d["action"] == "EXIT_GIVEBACK", (
        f"past the runner line, giving back 36% of peak must trip the "
        f"tightened 30% limit — got {d['action']}: {d['detail']}")
    assert policy["giveback_pct_runner"] == 30.0
    assert policy["giveback_runner_min_r"] == 1.0


def test_giveback_runner_tier_would_NOT_have_fired_under_the_old_flat_50pct():
    """
    Same fixture as the test above, replayed with giveback_pct_runner
    forced back to 50 — demonstrating the check actually distinguishes the
    two behaviours rather than passing either way.
    """
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    entry, stop = 100.0, 94.0
    hwm = entry + 1.4 * (entry - stop)
    ltp = entry + 0.9 * (entry - stop)
    pos = {"symbol": "X", "entry_price": entry, "planned_stop": stop,
           "active_sl": stop, "high_water_mark": hwm, "current_qty": 10,
           "actual_qty": 10, "framework": "SWING", "direction": "LONG",
           "partial_booked_qty": 5}
    with cfg_ctx({}):
        policy = load_exit_policy()
    policy["giveback_pct_runner"] = 50.0   # simulate the pre-fix flat setting
    d = evaluate_exit(pos, ltp, 5, policy)
    assert d["action"] != "EXIT_GIVEBACK", (
        "sanity check on the test fixture itself: at the OLD flat 50% "
        "setting this exact position must NOT exit, or the test above is "
        "not actually proving the tiering changed anything")


TESTS = [
    ("rank floor blocks travelfood-shaped negative rank",
     test_rank_floor_blocks_travelfood_shaped_negative_rank),
    ("rank floor admits an ordinary positive rank",
     test_rank_floor_admits_an_ordinary_positive_rank),
    ("rank floor is independent of the relative gate",
     test_rank_floor_is_independent_of_the_relative_gate),
    ("regime min_rr reads the regime-specific override",
     test_regime_min_rr_reads_the_regime_specific_override),
    ("regime min_rr falls back to the flat default",
     test_regime_min_rr_falls_back_to_the_flat_default_for_an_unknown_regime),
    ("regime min_rr key convention matches space-separated regime",
     test_regime_min_rr_key_convention_matches_space_separated_regime),
    ("partial fires at the repriced 1.0R, not the old 1.5R",
     test_partial_fires_at_the_repriced_one_r_not_the_old_1_5r),
    ("breakeven engages at the repriced 0.5R",
     test_breakeven_engages_at_the_repriced_half_r),
    ("giveback stays loose below the runner line",
     test_giveback_stays_loose_below_the_runner_line),
    ("giveback tightens once a partial should be banked",
     test_giveback_tightens_once_a_partial_should_be_banked),
    ("giveback runner tier would not have fired under the old flat 50%",
     test_giveback_runner_tier_would_NOT_have_fired_under_the_old_flat_50pct),
]
