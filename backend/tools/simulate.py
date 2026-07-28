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
from config import get_supabase, DRY_RUN, is_kill_switch_active


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


def simulate_intraday(sb, force_phase: str | None = None) -> dict:
    _hdr("INTRADAY")
    from intraday.engine import IntradayEngine
    from intraday.session import session_state
    from intraday import market_context as mkt
    from intraday.exit_policy import evaluate_intraday_exit, load_intraday_policy
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
        ok_s, why_s, _ = gate_for_framework(
            "INTRADAY", [b.high for b in ctx.bars], [b.low for b in ctx.bars])
        if not ok_s:
            blocked_struct += 1
            logger.info(f"      {sym:<12} {best.strategy}  BLOCKED structure — {why_s[:52]}")
            continue
        qty = int((TOTAL_CAPITAL * mc.size_multiplier) // best.entry) if best.entry else 0
        ok_c, why_c = is_worth_taking(best.entry, qty, best.target, best.stop) if qty else (False, "no size")
        if not ok_c:
            blocked_cost += 1
            logger.info(f"      {sym:<12} {best.strategy}  BLOCKED cost — {why_c[:52]}")
            continue
        passed += 1
        logger.info(f"    → {sym:<12} {best.strategy}  TAKEABLE  entry {best.entry} "
                    f"stop {best.stop} target {best.target} R:R {best.rr:.1f} conf {best.confidence}")

    logger.info(f"\n      {fired} fired · {blocked_event} event · {blocked_struct} structure "
                f"· {blocked_cost} cost · {passed} takeable")

    # Intraday positions against the intraday policy.
    ipos = [p for p in eng.positions
            if (p.get("framework") or "").upper() == "INTRADAY"]
    logger.info(f"\n  INTRADAY POSITIONS ({len(ipos)}):")
    ipol = load_intraday_policy()
    for p in ipos:
        ltp = float(p.get("current_price") or p.get("entry_price") or 0)
        d = evaluate_intraday_exit(p, ltp, ipol)
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
