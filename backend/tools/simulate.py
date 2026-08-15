"""
Run both frameworks end to end and report what they would do, changing nothing.

    python -m tools.simulate

WHY THIS EXISTS SEPARATELY FROM PAPER MODE
-------------------------------------------
Paper mode answers "what happens over weeks". This answers "is the machinery
wired correctly RIGHT NOW" — every gate, every policy, every threshold,
evaluated against live data and printed, in one pass, without writing anything.

It exists because most of the failures in this project have not been wrong
decisions. They have been decisions that never ran: a filter that returned zero
rows without raising, a monitor watching nothing, a column read that was always
NULL, an order retried forever. None of those show up in a unit test and all of
them show up here, because this prints what each stage actually produced rather
than whether it completed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, DRY_RUN, is_kill_switch_active, fetch_all


def _hdr(t: str) -> None:
    logger.info("")
    logger.info("─" * 70)
    logger.info(t)
    logger.info("─" * 70)


def simulate_swing(sb) -> dict:
    _hdr("SWING")
    from control.position_lifecycle import evaluate_exit, load_exit_policy
    from control.exit_rules import load_signal_context, assess_trend
    from execution.gates import trading_mode, auto_exit_enabled
    from analysis.trade_decision import decide

    mode = trading_mode("SWING")
    logger.info(f"  mode={mode}  auto-exit={'ON' if auto_exit_enabled('SWING') else 'off'}")

    rows = [r for r in (sb.table("open_positions").select("*")
                        .eq("status", "ACTIVE").execute().data or [])
            if (r.get("framework") or "SWING").upper() == "SWING"]
    pol = load_exit_policy()
    pol["_trend_ctx"] = load_signal_context(sb, [r["symbol"] for r in rows])

    logger.info(f"\n  POSITIONS ({len(rows)}) — what the exit engine says at the live price:")
    acted = 0
    for p in rows:
        ltp = float(p.get("current_price") or p.get("entry_price") or 0)
        d = evaluate_exit(p, ltp, 5, pol)
        tq = assess_trend(pol["_trend_ctx"].get(p["symbol"], {}), p)
        entry, stop = float(p.get("entry_price") or 0), float(p.get("planned_stop") or 0)
        risk = (entry - stop) if stop and stop < entry else entry * 0.05
        r_now = (ltp - entry) / risk if risk else 0
        mark = "→" if d["action"] != "HOLD" else " "
        if d["action"] != "HOLD":
            acted += 1
        logger.info(f"   {mark} {p['symbol']:<11} {r_now:+.2f}R  {d['action']:<15} "
                    f"trend={tq.verdict} ({tq.score:.0%})")
        logger.info(f"       {d['detail'][:96]}")

    # Today's plans, evaluated as the morning would.
    latest = (sb.table("signal_output_daily").select("date")
                .order("date", desc=True).limit(1).execute().data or [])
    plans = []
    if latest:
        plans = (sb.table("signal_output_daily").select("*")
                   .eq("date", latest[0]["date"]).execute().data or [])
    verdicts = {}
    for c in plans:
        d = decide(c, None)
        verdicts[d.action] = verdicts.get(d.action, 0) + 1
    logger.info(f"\n  TRADE PLANS ({len(plans)} for {latest[0]['date'] if latest else '?'}):")
    for k, v in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        logger.info(f"      {k:<12} {v}")
    buyable = verdicts.get("BUY_NOW", 0) + verdicts.get("CHASE_LIMIT", 0)
    logger.info(f"      -> {buyable} buyable at the last close")
    return {"positions": len(rows), "actions": acted, "plans": len(plans),
            "buyable": buyable, "mode": mode}


def engine_report(sb) -> None:
    """
    What each intraday engine has actually produced, from resolved outcomes.

    The gates and thresholds tuned this week are assertions until this table
    disagrees with them. It reads intraday_setups, where every DETECTION is
    recorded with why it was taken or declined and what then happened — so it
    answers the question the P&L cannot: was declining right?
    """
    _hdr("INTRADAY ENGINE SCORECARD (resolved outcomes)")
    from collections import defaultdict
    # PAGED — this read has NO date filter at all, so it is the whole table
    # (8324 rows on 15-Aug-2026) and was returning an arbitrary 1000 of them.
    # This is the read-only preview CLAUDE.md tells you to run before changing
    # anything, so a scorecard computed from a silently truncated twelfth of
    # the evidence is the worst possible place for this defect to hide.
    rows = fetch_all(lambda: sb.table("intraday_setups")
                     .select("strategy,cost_verdict,outcome,outcome_pct,confidence"))
    done = [r for r in rows if r.get("outcome")]
    if not done:
        logger.info("  no resolved outcomes yet — they are written at session close")
        return

    by_eng = defaultdict(lambda: defaultdict(int))
    for r in done:
        by_eng[r.get("strategy") or "?"][r["outcome"]] += 1
    logger.info(f"  {'engine':<8}{'TARGET':>8}{'STOP':>7}{'TIME':>7}{'n':>6}   hit rate")
    for eng, o in sorted(by_eng.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(o.values()); t = o.get("TARGET", 0)
        flag = "  <- review" if n >= 10 and t / n < 0.15 else ""
        logger.info(f"  {eng:<8}{t:>8}{o.get('STOP',0):>7}{o.get('TIMEOUT',0):>7}{n:>6}"
                    f"   {t/n:>6.0%}{flag}")

    by_v = defaultdict(lambda: defaultdict(int))
    for r in done:
        by_v[r.get("cost_verdict") or "?"][r["outcome"]] += 1
    logger.info("")
    logger.info("  WAS DECLINING RIGHT? — target-hit rate among setups we refused:")
    for v, o in sorted(by_v.items()):
        n = sum(o.values()); t = o.get("TARGET", 0)
        logger.info(f"    {v:<20} {t}/{n} would have reached target ({t/n:.0%})")


def simulate_swing_entries(sb) -> dict:
    """
    What auto-entry WOULD take tonight, and why it preferred those names.

    Answers the question the aggregate cannot: given eight buyable plans and two
    entries, which two, and what beat what. Every gate that removes a name is
    printed with its reason, because a plan silently absent from the result is
    indistinguishable from one that was never considered.
    """
    _hdr("SWING AUTO-ENTRY (dry run)")
    from analysis.trade_decision import decide
    from analysis.entry_ranking import rank
    from execution.gates import trading_mode
    from execution import paper_broker
    from config import TOTAL_CAPITAL, cfg_bool, cfg_int

    on   = cfg_bool("swing_auto_entry", False)
    live = trading_mode("SWING") == "LIVE"
    mx   = cfg_int("swing_max_new_per_day", 2)
    logger.info(f"  swing_auto_entry={'ON' if on else 'off'} · mode={trading_mode('SWING')} "
                f"· max {mx}/day")
    if live:
        logger.warning("  SWING is LIVE — auto-entry refuses to run (not implemented "
                       "for live). This shows what it WOULD do in PAPER.")

    latest = (sb.table("signal_output_daily").select("date")
                .order("date", desc=True).limit(1).execute().data or [])
    if not latest:
        logger.info("  no plans")
        return {"would_take": 0}
    day = latest[0]["date"]
    plans = (sb.table("signal_output_daily").select("*")
               .eq("date", day).execute().data or [])
    open_rows = (sb.table("open_positions").select("*")
                   .eq("status", "ACTIVE").execute().data or [])
    held = {r["symbol"] for r in open_rows}
    regime = plans[0].get("regime") if plans else "NEUTRAL"

    ranked = {r.symbol: r for r in rank(plans)}
    plans.sort(key=lambda p: -(ranked[p["symbol"]].total if p.get("symbol") in ranked else 0))

    logger.info("")
    logger.info(f"  {len(plans)} plans for {day}, ranked by the composite:")
    taken, considered = [], 0
    for p in plans:
        sym = p.get("symbol")
        if not sym:
            continue
        rk = ranked.get(sym)
        if sym in held:
            continue
        d = decide(p, None, total_capital=TOTAL_CAPITAL,
                   open_positions=open_rows, regime=regime,
                   max_chase_pct=p.get("ai_max_chase_pct") or None)
        if d.action not in ("BUY_NOW", "CHASE_LIMIT"):
            continue
        considered += 1
        qty = int(d.qty or 0)
        if qty <= 0:
            logger.info(f"      {sym:<12} rank {rk.total:>6.1f}  SKIP — no size at this price")
            continue
        allowed, why, _ = paper_broker.capacity("SWING", sb)
        if not allowed:
            logger.info(f"      {sym:<12} rank {rk.total:>6.1f}  SKIP — {why}")
            break
        if len(taken) >= mx:
            logger.info(f"      {sym:<12} rank {rk.total:>6.1f}  not taken — daily cap "
                        f"of {mx} already filled by higher-ranked names")
            continue
        taken.append(sym)
        logger.info(f"    → {sym:<12} rank {rk.total:>6.1f}  TAKE {qty} @ ~{d.live_price} "
                    f"stop {d.stop} target {d.target}")
        logger.info(f"        why: {rk.why()}")

    logger.info("")
    logger.info(f"      {considered} buyable · would take {len(taken)}: "
                f"{', '.join(taken) or 'none'}")
    return {"would_take": len(taken), "considered": considered, "symbols": taken}


def simulate_intraday(sb, force_phase: str | None = None) -> dict:
    _hdr("INTRADAY")
    from intraday.engine import IntradayEngine
    from intraday.session import session_state
    from intraday import market_context as mkt
    from intraday.exit_policy import (evaluate_intraday_exit, load_intraday_policy,
                                  last_completed_close)
    from intraday.strategies.registry import evaluate_all, engine_names
    from intraday.cost_model import is_worth_taking
    from execution.gates import trading_mode, auto_exit_enabled
    from config import TOTAL_CAPITAL

    mode = trading_mode("INTRADAY")
    st = session_state()
    # A simulation that only runs 09:30-15:00 is nearly useless — most of the
    # time you want to check the wiring is outside market hours. --phase
    # evaluates the engines as if it were that part of the session; prices are
    # still the last real ones, so the setups are real, only the clock is not.
    phase = force_phase or st.phase
    if force_phase:
        logger.warning(f"  --phase {force_phase}: evaluating as if the session were "
                       f"{force_phase} (real prices, simulated clock)")
    logger.info(f"  mode={mode}  auto-exit={'ON' if auto_exit_enabled('INTRADAY') else 'off'}")
    logger.info(f"  session: {st.phase}  can_enter={st.can_enter} — {st.reason}")
    logger.info(f"  engines: {', '.join(engine_names())}")

    eng = IntradayEngine(sb)
    eng.load_state()
    n_uni = eng.refresh_universe()
    n_ctx = eng.refresh_contexts()
    logger.info(f"  universe={n_uni}  contexts={n_ctx}  watching={len(eng.watch_symbols())}")

    mc = mkt.from_context(eng._index_ctx)
    logger.info(f"  market: {mc.state} longs={mc.allow_longs} size x{mc.size_multiplier}")
    logger.info(f"          {mc.reason[:92]}")

    # Every engine over every symbol, with each gate reported.
    logger.info("\n  ENGINE SCAN — what fires, and what each gate does to it:")
    from analysis.market_structure import gate_for_framework
    from intraday.news_gate import NewsGate
    ng = NewsGate(sb)
    ng.refresh(list(eng._contexts))

    fired = blocked_struct = blocked_event = blocked_cost = passed = 0
    for sym, ctx in (eng._contexts or {}).items():
        best, _all = evaluate_all(ctx, phase)
        if not best:
            continue
        fired += 1
        ev = ng.check(sym, ctx.sector)
        if not ev.allow:
            blocked_event += 1
            logger.info(f"      {sym:<12} {best.strategy}  BLOCKED event — {ev.reason[:52]}")
            continue
        # direction=best.direction on both gates below: without it, a SHORT
        # setup was structurally judged by gate_long (which blocks a
        # downtrend — exactly the structure a short wants) and cost-checked
        # as a LONG (whose stop/target shape a short's levels fail by
        # construction, refused as "wrong side of entry for a LONG"). This is
        # the read-only preview tool CLAUDE.md tells the operator to run
        # before trusting a session; leaving it long-only here would have
        # reported every short setup as blocked or uneconomic when it was
        # neither — the same "quiet market" illusion the live engine's own
        # missing direction= argument produced, caught here before it shipped.
        ok_s, why_s, _ = gate_for_framework(
            "INTRADAY", [b.high for b in ctx.bars], [b.low for b in ctx.bars],
            direction=best.direction)
        if not ok_s:
            blocked_struct += 1
            logger.info(f"      {sym:<12} {best.strategy}  BLOCKED structure — {why_s[:52]}")
            continue
        # capital_for("INTRADAY"): TOTAL_CAPITAL alone predates the swing/
        # intraday capital split and no longer matches what the live engine
        # actually sizes against once intraday_capital diverges from it —
        # this preview would silently show a different quantity than the
        # session it is meant to preview.
        from config import capital_for
        qty = int((capital_for("INTRADAY") * mc.size_multiplier) // best.entry) if best.entry else 0
        ok_c, why_c = is_worth_taking(best.entry, qty, best.target, best.stop,
                                      direction=best.direction) if qty else (False, "no size")
        if not ok_c:
            blocked_cost += 1
            logger.info(f"      {sym:<12} {best.strategy}  BLOCKED cost — {why_c[:52]}")
            continue
        passed += 1
        logger.info(f"    → {sym:<12} {best.strategy} {best.direction:<5} TAKEABLE  "
                    f"entry {best.entry} stop {best.stop} target {best.target} "
                    f"R:R {best.rr:.1f} conf {best.confidence}")

    logger.info(f"\n      {fired} fired · {blocked_event} event · {blocked_struct} structure "
                f"· {blocked_cost} cost · {passed} takeable")

    # Intraday positions against the intraday policy.
    ipos = [p for p in eng.positions
            if (p.get("framework") or "").upper() == "INTRADAY"]
    logger.info(f"\n  INTRADAY POSITIONS ({len(ipos)}):")
    ipol = load_intraday_policy()
    for p in ipos:
        ltp = float(p.get("current_price") or p.get("entry_price") or 0)
        # Same last-completed-bar close the daemon passes. A preview tool
        # that omits it would report HOLD where the daemon exits (or the
        # reverse) the moment intraday_invalidation_require_close is on —
        # the class of divergence tools/simulate.py exists to prevent.
        _c = eng._contexts.get(p["symbol"])
        d = evaluate_intraday_exit(
            p, ltp, ipol,
            last_close=last_completed_close(getattr(_c, "bars", None) or []))
        logger.info(f"      {p['symbol']:<12} {d['action']:<18} {d['detail'][:70]}")
    if not ipos:
        logger.info("      none — expected until a paper entry is taken during a session")

    return {"universe": n_uni, "contexts": n_ctx, "fired": fired,
            "takeable": passed, "market": mc.state, "mode": mode}


def main(force_phase: str | None = None) -> int:
    logger.info("═" * 70)
    logger.info("TradeOS — full simulation (read-only)")
    logger.info("═" * 70)
    sb = get_supabase()
    logger.info(f"  DRY_RUN={DRY_RUN}  kill_switch={is_kill_switch_active()}")

    sw = simulate_swing(sb)
    simulate_swing_entries(sb)
    engine_report(sb)
    it = simulate_intraday(sb, force_phase)

    _hdr("SUMMARY")
    logger.info(f"  SWING     {sw['mode']:<6} {sw['positions']} positions, "
                f"{sw['actions']} needing action, {sw['buyable']} buyable plans")
    logger.info(f"  INTRADAY  {it['mode']:<6} {it['universe']} universe, "
                f"{it['fired']} setups fired, {it['takeable']} takeable, market {it['market']}")
    logger.info("")
    logger.info("  Nothing was written. This is what the system WOULD do right now.")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Simulate both frameworks, read-only")
    ap.add_argument("--phase", choices=["OPENING", "PRIME", "DRIFT", "AFTERNOON", "CLOSING"],
                    help="evaluate the engines as if the session were in this phase")
    sys.exit(main(ap.parse_args().phase))
