"""
Swing paper entries — so the swing framework can learn without spending.

WHY THIS WAS MISSING
--------------------
swing_auto_entry existed as a config key and NOTHING read it. Intraday could
take a simulated entry and therefore produce complete round trips; swing could
only ever simulate the exit half of a trade it had not taken. A framework judged
solely on how it leaves positions tells you nothing about which it should have
entered, and entry quality is where the swing edge actually lives — the exit
tuning was measured at roughly +1% per trade while the entry gate was the reason
46% of trades never reached +2% at all.

WHAT IT TAKES, AND WHAT IT REFUSES
----------------------------------
Reads the same signal_output_daily plans the digest shows you, runs them through
the same decide() the dashboard and Telegram use, and takes only what comes back
BUY_NOW or CHASE_LIMIT. It cannot see anything you cannot: no separate screen,
no privileged data, no different thresholds. If it and the digest ever disagree
about a symbol, one of them is broken.

Portfolio constraints are enforced, not bypassed. A paper run that ignores
sector caps and position limits would produce an equity curve the real account
could never have achieved, which is the specific way paper trading lies.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (IST, TOTAL_CAPITAL, cfg_bool, cfg_int, get_supabase,
                    today_ist, is_kill_switch_active)


def run(sb=None, notifier=None) -> dict:
    """
    Take today's buyable swing plans as PAPER positions.

    Called from the evening pipeline after the snapshot, and safe to re-run: a
    symbol already held is skipped, so a second run adds nothing.
    """
    sb = sb or get_supabase()
    result = {"considered": 0, "taken": 0, "skipped": 0, "reasons": {}}

    if is_kill_switch_active():
        return {**result, "status": "kill_switch"}

    from execution.gates import is_paper, trading_mode
    if not cfg_bool("swing_auto_entry", False):
        return {**result, "status": "disabled"}

    # LIVE auto-entry needs its own switch. Promoting swing from paper to live
    # must not silently also promote "simulate an entry" into "spend money".
    live = not is_paper("SWING")
    if live and not cfg_bool("swing_live_auto_entry", False):
        logger.warning("  swing_auto_entry is on but the framework is LIVE and "
                       "swing_live_auto_entry is off — taking nothing. Set that "
                       "second switch deliberately, or move swing to PAPER.")
        return {**result, "status": "live_entry_not_permitted"}

    from analysis.trade_decision import decide
    from execution import paper_broker

    latest = (sb.table("signal_output_daily").select("date")
                .order("date", desc=True).limit(1).execute().data or [])
    if not latest:
        return {**result, "status": "no_plans"}
    day = latest[0]["date"]
    plans = (sb.table("signal_output_daily").select("*")
               .eq("date", day).execute().data or [])

    open_rows = (sb.table("open_positions").select("*")
                   .eq("status", "ACTIVE").execute().data or [])
    held = {r["symbol"] for r in open_rows}
    regime = plans[0].get("regime") if plans else "NEUTRAL"
    max_new = cfg_int("swing_max_new_per_day", 2)

    # Rank by the system's own conviction so a cap takes the best, not the
    # first alphabetically.
    plans.sort(key=lambda p: -(p.get("final_score") or p.get("score") or 0))

    for p in plans:
        if result["taken"] >= max_new:
            break
        sym = p.get("symbol")
        if not sym or sym in held:
            continue
        result["considered"] += 1

        d = decide(p, None, total_capital=TOTAL_CAPITAL,
                   open_positions=open_rows, regime=regime,
                   max_chase_pct=p.get("ai_max_chase_pct") or None)
        if d.action not in ("BUY_NOW", "CHASE_LIMIT"):
            result["skipped"] += 1
            result["reasons"][d.action] = result["reasons"].get(d.action, 0) + 1
            continue

        qty = int(d.qty or 0)
        if qty <= 0:
            result["skipped"] += 1
            result["reasons"]["NO_SIZE"] = result["reasons"].get("NO_SIZE", 0) + 1
            continue

        allowed, why, _left = paper_broker.capacity("SWING", sb)
        if not allowed:
            logger.info(f"  📄 swing paper skip {sym} — {why}")
            break

        f = paper_broker.simulate_fill(sym, "BUY", qty, "LIMIT",
                                       d.live_price, d.live_price)
        if not f.ok:
            result["skipped"] += 1
            continue

        if paper_broker.open_position(
                sym, qty, f.fill_price,
                {"stop": d.stop, "target": d.target, "strategy": p.get("strategy")},
                "SWING", sb, charges=f.charges):
            result["taken"] += 1
            held.add(sym)
            open_rows.append({"symbol": sym, "sector": p.get("sector"),
                              "invested_value": f.fill_price * qty})
            if notifier:
                try:
                    from intraday.notifier import Action
                    notifier.send(Action(
                        sym, "PAPER_ENTRY",
                        f"[PAPER] bought {qty} @ ₹{f.fill_price:,.2f}",
                        f"{d.reason}\nStop ₹{d.stop} · target ₹{d.target}. "
                        f"Simulated — no real order.",
                        ltp=f.fill_price, urgency="INFO"), force=True)
                except Exception:
                    pass

    logger.info(f"  swing paper entries for {day}: {result['taken']} taken of "
                f"{result['considered']} considered"
                + (f" — skipped: {result['reasons']}" if result["reasons"] else ""))
    return {**result, "status": "ok", "date": day, "mode": trading_mode("SWING")}


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
