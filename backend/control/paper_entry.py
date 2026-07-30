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

    if live:
        # This module has only ever written through paper_broker, and
        # open_position() hardcodes mode='PAPER'. Letting a LIVE framework reach
        # that code would create simulated positions inside a live book while
        # the operator believed real orders had been placed — the worst of both:
        # no exposure taken on a plan you think you own, and a book whose P&L
        # mixes fills that never happened with fills that did.
        #
        # Live entry is not implemented anywhere, for either framework. Entries
        # commit new capital on a judgement call; exits reduce exposure on a
        # position that already exists and are bounded by what is held. That is
        # why exits are automated and entries are not.
        # Live entry EXISTS now, but not here. It lives in the daemon —
        # IntradayEngine._maybe_enter_swing — because this module runs after the
        # close and preflight requires an open market: a real order from here is
        # rejected MARKET_CLOSED every time.
        #
        # This path still refuses rather than simulating, because open_position()
        # hardcodes mode='PAPER' and writing simulated positions into a live book
        # is the worst outcome available: no exposure on a plan you believe you
        # own, and a P&L mixing fills that happened with fills that did not.
        logger.info(
            "  swing is LIVE — entries are taken intraday by the daemon on live "
            "prices, not by this evening pass. Nothing to do here.")
        return {**result, "status": "live_entry_handled_by_daemon"}

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

    # Rank on everything the pipeline computed, not just the screener score.
    #
    # final_score is the SCREENER's verdict, formed before the AI reviewed the
    # name, before R:R was measured at today's price, and before entry timing
    # was classified. Sorting on it alone discarded 27 steps of analysis at the
    # exact moment it mattered — choosing which two of the day's plans get the
    # only two entries available.
    #
    # On 29 Jul it ranked AJANTPHARM first at 71. The composite puts it seventh,
    # because it has a results event inside a day. The old order would have
    # opened a position directly into it.
    from analysis.entry_ranking import rank as _rank
    _ranked = {r.symbol: r for r in _rank(plans)}
    plans.sort(key=lambda p: -(_ranked[p["symbol"]].total
                               if p.get("symbol") in _ranked else 0))

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

        rk = _ranked.get(sym)
        if paper_broker.open_position(
                sym, qty, f.fill_price,
                {"stop": d.stop, "target": d.target, "strategy": p.get("strategy"),
                 # WHY this name and not the other seven. Recorded on the
                 # position so the dashboard can show the reasoning beside the
                 # trade rather than leaving it in a log nobody opens.
                 "entry_rationale": (f"rank {rk.total:.0f} — {rk.why()}" if rk else None)},
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
                        ltp=f.fill_price, urgency="INFO",
                        framework="SWING"), force=True)
                except Exception:
                    pass

    logger.info(f"  swing paper entries for {day}: {result['taken']} taken of "
                f"{result['considered']} considered"
                + (f" — skipped: {result['reasons']}" if result["reasons"] else ""))
    return {**result, "status": "ok", "date": day, "mode": trading_mode("SWING")}


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
