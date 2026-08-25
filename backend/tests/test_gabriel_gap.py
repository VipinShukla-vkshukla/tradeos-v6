"""
The GABRIEL gap: a trade that was refused three days running, bought on the
fourth, never once traded above its entry price, and was finally closed by hand.

Every check here is anchored to a real number from that position or from SCI,
which was on the same path four sessions later. Nothing is a synthetic example.

    GABRIEL   entered 06-Aug-2026 @ 1554.80, stop 1403.97 (-9.7%), 2 shares
              high-water mark 1555.20 -> peak +0.003R, MFE +0.03%
              closed 17-Aug @ 1432.60, -7.86%, EXIT_STALL at "11 sessions"
              (it had been held 8)
    SCI       entered 10-Aug-2026 @ 300.60, stop 279.14, 14 shares
              high-water mark 301.60 -> peak +0.047R
              17-Aug: 287.50, -4.36%, -0.61R
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tests import cfg_ctx

IST = timezone(timedelta(hours=5, minutes=30))

# GABRIEL's real numbers.
G_ENTRY, G_STOP, G_HWM = 1554.80, 1403.97, 1555.20
G_RISK = G_ENTRY - G_STOP          # 150.83


def _pos(entry, stop, hwm, **kw) -> dict:
    p = {"symbol": kw.pop("symbol", "GABRIEL"), "entry_price": entry,
         "active_sl": stop, "planned_stop": stop, "high_water_mark": hwm,
         "current_qty": 2, "actual_qty": 2, "framework": "SWING",
         "direction": "LONG", "target_price": 1730.40}
    p.update(kw)
    return p


def _policy(**over) -> dict:
    from control.position_lifecycle import load_exit_policy
    with cfg_ctx({}):
        pol = load_exit_policy()
    pol.update(over)
    return pol


# ── 1. One clock ────────────────────────────────────────────────────────────

def test_sessions_between_counts_sessions_not_calendar_days():
    """06-Aug to 17-Aug is 11 calendar days and 8 trading sessions."""
    from control.position_lifecycle import sessions_between
    cal = ["2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12",
           "2026-08-13", "2026-08-14", "2026-08-17"]
    n = sessions_between(cal, "2026-08-06", "2026-08-17")
    assert n == 7, (
        f"06-Aug entry to 17-Aug is 7 sessions AFTER the entry day, got {n} "
        f"— the daemon reported 11 by counting calendar days")
    cal_days = (date(2026, 8, 17) - date(2026, 8, 6)).days
    assert cal_days == 11 and n != cal_days, \
        "the whole point is that these two numbers differ"


def test_sessions_between_understates_when_the_calendar_is_missing():
    """
    An unavailable calendar must not silently become a different clock. It
    understates, because an exit that fires late is recoverable and one that
    fires early on bad arithmetic is not.
    """
    from control.position_lifecycle import sessions_between
    n = sessions_between([], "2026-08-06", "2026-08-17")
    assert n <= 8, f"fallback must not exceed the true session count, got {n}"
    assert n > 0, "fallback must still advance, or time-based exits never fire"


# ── 2. EXIT_FASTFAIL ────────────────────────────────────────────────────────

def test_fastfail_would_have_caught_gabriel_on_day_four():
    """10-Aug: close 1469.80, peak still 1555.20. -0.56R with a 0.00R peak."""
    from control.position_lifecycle import evaluate_exit
    with cfg_ctx({}):
        d = evaluate_exit(_pos(G_ENTRY, G_STOP, G_HWM), 1469.80, 4,
                          _policy(fastfail_enabled=True))
    assert d["action"] == "EXIT_FASTFAIL", (
        f"GABRIEL at 4 sessions, -5.5%, peak +0.00R must fast-fail, got "
        f"{d['action']}: {d['detail']}")


def test_fastfail_would_catch_sci_today():
    """SCI: entry 300.60, stop 279.14, peak 301.60, now 287.50 = -0.61R."""
    from control.position_lifecycle import evaluate_exit
    with cfg_ctx({}):
        d = evaluate_exit(_pos(300.60, 279.14, 301.60, symbol="SCI",
                               target_price=325.85),
                          287.50, 5, _policy(fastfail_enabled=True))
    assert d["action"] == "EXIT_FASTFAIL", (
        f"SCI is the same shape as GABRIEL and must fast-fail, got {d['action']}")


def test_fastfail_is_off_unless_switched_on():
    """It sells while the ordinary stop is still far away. That needs a switch."""
    from control.position_lifecycle import evaluate_exit
    with cfg_ctx({}):
        d = evaluate_exit(_pos(G_ENTRY, G_STOP, G_HWM), 1469.80, 4,
                          _policy(fastfail_enabled=False))
    assert d["action"] != "EXIT_FASTFAIL", \
        "the default must not close positions"


def test_fastfail_spares_a_trade_that_worked_and_gave_it_back():
    """
    The give-back guard owns that case. A position that ran to +1.5R and came
    back to -0.6R must NOT be fast-failed — its peak clears the bar.
    """
    from control.position_lifecycle import evaluate_exit
    hwm = G_ENTRY + 1.5 * G_RISK
    with cfg_ctx({}):
        d = evaluate_exit(_pos(G_ENTRY, G_STOP, hwm), 1469.80, 6,
                          _policy(fastfail_enabled=True))
    assert d["action"] != "EXIT_FASTFAIL", (
        f"a position that peaked at +1.5R has worked; fast-fail is for trades "
        f"that never did — got {d['action']}")


def test_fastfail_spares_a_trade_that_is_merely_flat():
    """-0.1R with no peak is going nowhere, not failing. The stall rule owns it."""
    from control.position_lifecycle import evaluate_exit
    with cfg_ctx({}):
        d = evaluate_exit(_pos(G_ENTRY, G_STOP, G_HWM),
                          G_ENTRY - 0.1 * G_RISK, 5,
                          _policy(fastfail_enabled=True))
    assert d["action"] != "EXIT_FASTFAIL", \
        f"a flat trade must be left to the stall rule, got {d['action']}"


def test_fastfail_waits_the_full_four_sessions():
    from control.position_lifecycle import evaluate_exit
    with cfg_ctx({}):
        d = evaluate_exit(_pos(G_ENTRY, G_STOP, G_HWM), 1469.80, 3,
                          _policy(fastfail_enabled=True))
    assert d["action"] != "EXIT_FASTFAIL", \
        "three sessions is inside the normal wobble of an entry"


# ── 3. Frozen plan levels ───────────────────────────────────────────────────

def test_frozen_levels_stay_live_between_stop_and_target():
    from swing.compute.compute_msl import plan_levels_still_live
    prior = {"planned_stop": 1248.49, "planned_target": 1548.82}
    assert plan_levels_still_live(prior, 1414.80), \
        "GABRIEL's 29-Jul plan was live at its own birth price"
    assert plan_levels_still_live(prior, 1392.00), \
        "30-Jul: price fell, plan still live and R:R should IMPROVE"


def test_frozen_levels_die_once_price_passes_the_target():
    """
    This is the GABRIEL case. By 06-Aug the stock was at 1554.80 — above the
    29-Jul plan's own target of 1548.82. There is no trade left to inherit.
    """
    from swing.compute.compute_msl import plan_levels_still_live
    prior = {"planned_stop": 1248.49, "planned_target": 1548.82}
    assert not plan_levels_still_live(prior, 1554.80), (
        "price above the plan's target means the move already happened — "
        "the plan must expire, not follow the price up")


def test_frozen_levels_die_once_price_breaks_the_stop():
    from swing.compute.compute_msl import plan_levels_still_live
    prior = {"planned_stop": 1248.49, "planned_target": 1548.82}
    assert not plan_levels_still_live(prior, 1240.00), \
        "a broken thesis must produce a new plan, not inherit a rejected level"


def test_frozen_levels_make_rr_decay_as_price_runs():
    """
    THE WHOLE POINT. With levels frozen, R:R falls as price rises — which is
    what min_rr_to_enter was always meant to be reading. With levels
    re-anchored nightly it was pinned at 0.777 across a 4% price range.
    """
    from swing.compute.compute_msl import compute_trade_plan
    prior = {"planned_stop": 1248.49, "planned_target": 1548.82,
             "planned_stop_source": "structure", "date": "2026-07-29"}
    with cfg_ctx({"plan_levels_frozen": "true"}):
        at_birth = compute_trade_plan(
            {"atr_14": 67.0, "close": 1414.80, "supertrend": 1300.0},
            1414.80, 1440.0, {"regime": "NEUTRAL"}, prior=prior)
        chased = compute_trade_plan(
            {"atr_14": 71.0, "close": 1527.40, "supertrend": 1400.0},
            1527.40, 1560.0, {"regime": "NEUTRAL"}, prior=prior)
    assert at_birth["expected_r"] > chased["expected_r"], (
        f"R:R must FALL as price runs away from a frozen plan: "
        f"{at_birth['expected_r']} at 1414.80 vs {chased['expected_r']} at 1527.40")
    assert chased["planned_stop"] == 1248.49, \
        "the stop must not move up with the price — that is the discipline"
    assert str(chased["planned_stop_source"]).startswith("frozen:"), \
        "an inherited level must be labelled as one"


# ── 4. Liquidity floor ──────────────────────────────────────────────────────

def test_swing_floor_refuses_gabriel_and_admits_hindcopper():
    from analysis.overlays import liquidity_ok
    cfg = {"overlay_liquidity_enabled": "true",
           "swing_liquidity_floor_enabled": "true", "swing_min_value_cr": "200"}
    with cfg_ctx(cfg):
        ok_g, why_g = liquidity_ok({"value_cr": 123.2, "atr_pct": 4.49},
                                   planned_value=3109.60, framework="SWING")
        ok_h, _ = liquidity_ok({"value_cr": 816.1, "atr_pct": 4.14},
                               planned_value=4189.20, framework="SWING")
    assert not ok_g, f"GABRIEL at Rs 123 cr must be refused: {why_g}"
    assert ok_h, "HINDCOPPER at Rs 816 cr must pass"


def test_swing_floor_does_not_touch_the_intraday_book():
    """Intraday is flat by 15:15 and never holds a gap. The floor is swing-only."""
    from analysis.overlays import liquidity_ok
    with cfg_ctx({"overlay_liquidity_enabled": "true",
                  "swing_liquidity_floor_enabled": "true",
                  "swing_min_value_cr": "200"}):
        ok, _ = liquidity_ok({"value_cr": 123.2, "atr_pct": 4.49},
                             planned_value=3109.60)
    assert ok, "no framework given must not apply the swing floor"


def test_the_old_share_test_alone_would_have_passed_gabriel():
    """
    Demonstrates why a second floor was needed: 2 shares of GABRIEL is 0.0003%
    of its turnover and clears every position-relative test easily.
    """
    from analysis.overlays import liquidity_ok
    with cfg_ctx({"overlay_liquidity_enabled": "true",
                  "swing_liquidity_floor_enabled": "false"}):
        ok, _ = liquidity_ok({"value_cr": 123.2, "atr_pct": 4.49},
                             planned_value=3109.60, framework="SWING")
    assert ok, "precondition: the share-of-turnover test alone admits GABRIEL"


# ── 5. The AI's own refusal ─────────────────────────────────────────────────

def test_avoid_entry_refuses():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_rank_respect_ai_avoid": "true"}):
        r = entry_refusals({"symbol": "GABRIEL", "eap_action": "AVOID_ENTRY",
                            "ai_risks": "Extended RSvN could lead to mean reversion"})
    assert r, "AVOID_ENTRY must refuse the entry"
    assert "AVOID_ENTRY" in r[0]


def test_a_normal_plan_is_not_refused():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_rank_respect_ai_avoid": "true",
                  "entry_respect_filter_reason": "true"}):
        assert not entry_refusals({"symbol": "HINDCOPPER", "eap_action": "NO_CHANGE",
                                   "filter_reason": "holding"})


def test_stale_insufficient_rr_no_longer_refuses_a_live_recovered_plan():
    """
    25-Aug-2026, superseding the original version of this test. GABRIEL's
    `insufficient_rr_0.78x` refusal (3-5 Aug) is no longer honoured via this
    path — decide()'s own `rr < min_rr` check already answers the identical
    question LIVE, every cycle, off the current price, and a plan only
    reaches entry_refusals() with a buyable decision after clearing that
    live bar. A frozen filter_reason string can then only ever be wrong
    (blocking a plan whose R:R has since recovered), never protective.
    Live case that forced this: ELGIEQUIP carried `insufficient_rr_0.75x`
    from the prior evening while today's live rr_live (1.34) had already
    cleared min_rr (1.0) — refused anyway, every 15s cycle, all day.
    `holding` and `lifecycle_reduce` remain states, not refusals, and must
    still not block anything.
    """
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_respect_filter_reason": "true"}):
        assert not entry_refusals({"symbol": "ELGIEQUIP",
                                   "filter_reason": "insufficient_rr_0.75x"})
        assert not entry_refusals({"symbol": "X", "filter_reason": "lifecycle_reduce"})


def test_filter_reason_hard_refusals_still_block():
    """
    The non-R:R-shaped refusal categories are untouched by the 25-Aug change
    above — only `insufficient_rr_*` was dropped, because only that category
    is a stale copy of something decide() already re-verifies live.
    """
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_respect_filter_reason": "true"}):
        assert entry_refusals({"symbol": "X", "filter_reason": "blocked_event_risk"})
        assert entry_refusals({"symbol": "X", "filter_reason": "rejected_liquidity"})
        assert entry_refusals({"symbol": "X", "filter_reason": "veto_sector_cap"})


def test_the_ai_can_veto_but_never_promote():
    """
    Asymmetry on purpose, matching the conviction rule: annotation, never
    promotion. A glowing review must not add rank.
    """
    from analysis.entry_ranking import score_plan
    base = {"symbol": "X", "final_score": 70.0}
    with cfg_ctx({}):
        plain = score_plan(base).total
        risky = score_plan({**base, "ai_risks": "extended, mean reversion likely"}).total
    assert risky < plain, "a stated risk must cost rank points"


# ── 6. The exit terminates ──────────────────────────────────────────────────

def test_exit_limit_scales_with_volatility():
    """
    GABRIEL: ATR 4.68%. A flat 30bps put the limit at 1460.60, above a market
    that had already traded to 1447 in the first minute. The ATR term puts it
    where the tape actually went.
    """
    from execution.exit_orders import exit_limit_price
    with cfg_ctx({"exit_slip_bps": "30", "exit_slip_atr_frac": "0.25"}):
        flat_only = exit_limit_price(1465.10, None)
        scaled = exit_limit_price(1465.10, 4.68)
    assert abs(flat_only - 1460.7) < 0.5, f"flat floor should hold, got {flat_only}"
    assert scaled < 1450.0, (
        f"a 4.68% ATR name needs more than 30bps of buffer, got {scaled} "
        f"— the 09:15 tape traded 1447 and this must be at or below it")
    assert scaled < flat_only, "the ATR term must widen, never narrow, the buffer"


def test_exit_limit_floor_binds_on_a_quiet_name():
    from execution.exit_orders import exit_limit_price
    with cfg_ctx({"exit_slip_bps": "30", "exit_slip_atr_frac": "0.25"}):
        px = exit_limit_price(1000.0, 0.8)     # 0.25 * 0.8% = 0.20% < 0.30%
    assert abs(px - 997.0) < 0.2, f"the flat floor must bind at low ATR, got {px}"


def test_stale_exits_finds_the_gabriel_order_and_ignores_buys():
    from execution.exit_orders import stale_exits
    now = datetime(2026, 8, 17, 11, 48, tzinfo=IST)
    orders = [
        {"tradingsymbol": "GABRIEL", "transaction_type": "SELL", "status": "OPEN",
         "order_timestamp": datetime(2026, 8, 17, 9, 15, tzinfo=IST), "price": 1460.6},
        {"tradingsymbol": "TATATECH", "transaction_type": "BUY", "status": "OPEN",
         "order_timestamp": datetime(2026, 8, 17, 9, 15, tzinfo=IST), "price": 845.1},
        {"tradingsymbol": "SCI", "transaction_type": "SELL", "status": "COMPLETE",
         "order_timestamp": datetime(2026, 8, 17, 9, 15, tzinfo=IST), "price": 287.5},
    ]
    stale = stale_exits(orders, now, 300.0)
    assert len(stale) == 1, f"only the open SELL is stranded, got {len(stale)}"
    assert stale[0]["tradingsymbol"] == "GABRIEL"
    assert stale[0]["_age_s"] > 9000, "the GABRIEL order rested 2h33m"


def test_a_fresh_exit_is_not_stale():
    from execution.exit_orders import stale_exits
    now = datetime(2026, 8, 17, 9, 15, 30, tzinfo=IST)
    orders = [{"tradingsymbol": "GABRIEL", "transaction_type": "SELL",
               "status": "OPEN", "price": 1460.6,
               "order_timestamp": datetime(2026, 8, 17, 9, 15, tzinfo=IST)}]
    assert not stale_exits(orders, now, 60.0), \
        "an order 30 seconds old has not had its chance yet"


TESTS = [
    ("sessions_between counts sessions, not calendar days", test_sessions_between_counts_sessions_not_calendar_days),
    ("a missing calendar understates rather than diverges", test_sessions_between_understates_when_the_calendar_is_missing),
    ("fastfail catches GABRIEL on day four", test_fastfail_would_have_caught_gabriel_on_day_four),
    ("fastfail catches SCI today", test_fastfail_would_catch_sci_today),
    ("fastfail is off unless switched on", test_fastfail_is_off_unless_switched_on),
    ("fastfail spares a trade that worked and faded", test_fastfail_spares_a_trade_that_worked_and_gave_it_back),
    ("fastfail spares a merely flat trade", test_fastfail_spares_a_trade_that_is_merely_flat),
    ("fastfail waits the full four sessions", test_fastfail_waits_the_full_four_sessions),
    ("frozen levels stay live inside the plan", test_frozen_levels_stay_live_between_stop_and_target),
    ("frozen levels expire past the target", test_frozen_levels_die_once_price_passes_the_target),
    ("frozen levels expire through the stop", test_frozen_levels_die_once_price_breaks_the_stop),
    ("frozen levels make R:R decay as price runs", test_frozen_levels_make_rr_decay_as_price_runs),
    ("swing floor refuses GABRIEL, admits HINDCOPPER", test_swing_floor_refuses_gabriel_and_admits_hindcopper),
    ("swing floor does not touch intraday", test_swing_floor_does_not_touch_the_intraday_book),
    ("the share test alone would have passed GABRIEL", test_the_old_share_test_alone_would_have_passed_gabriel),
    ("AVOID_ENTRY refuses", test_avoid_entry_refuses),
    ("a normal plan is not refused", test_a_normal_plan_is_not_refused),
    ("stale insufficient_rr no longer refuses a live-recovered plan", test_stale_insufficient_rr_no_longer_refuses_a_live_recovered_plan),
    ("filter_reason hard refusals still block", test_filter_reason_hard_refusals_still_block),
    ("the AI can veto but never promote", test_the_ai_can_veto_but_never_promote),
    ("exit limit scales with volatility", test_exit_limit_scales_with_volatility),
    ("exit limit floor binds on a quiet name", test_exit_limit_floor_binds_on_a_quiet_name),
    ("stale_exits finds the GABRIEL order", test_stale_exits_finds_the_gabriel_order_and_ignores_buys),
    ("a fresh exit is not stale", test_a_fresh_exit_is_not_stale),
]
