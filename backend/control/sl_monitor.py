"""
TradeOS v7 — Intraday Stop-Loss Monitor  [DELEGATING SHIM]
===========================================================
Superseded by control/position_lifecycle.py. Kept as an entry point because
pipeline docs and workflow files reference it by name.

WHY IT WAS REPLACED

  1. IT NEVER RAN. fetch_live_prices() imported `kite.kite_client`, a module
     that did not exist anywhere in the repo. The ImportError was caught, the
     function returned {}, and main() hit `if not prices: return` on every
     single invocation. Not one stop was ever checked. Its Telegram helper was
     also broken independently — it called
     `alerts.send_alerts.send_telegram_message`, which does not exist (the real
     function is `send_message`), so even a working price feed would have
     produced silent alerts.

  2. NOTHING SCHEDULED IT. Four files documented a `pipeline_intraday.yml`
     running this every 30 minutes. That workflow was never in the repo.

  3. ITS TRAILING RULE WAS THE WRONG ONE. It trailed active_sl from day one:
         if trail_pct and ltp > hwm: new_sl = ltp * (1 - trail_pct/100)
     Measured over the 70 realised trades, `TRAIL SL HIT` accounted for 37 of
     70 exits at an average P&L of -0.40%, and compressed the average winner to
     +3.89% against an in-trade MFE of +5.90% for winners. Trailing from entry
     takes the stop out during ordinary retracement long before a 1-3 week
     thesis resolves. position_lifecycle delays trailing until the position is
     up exit_trail_after_r (2R by default).

  Two modules both writing open_positions.active_sl under DIFFERENT policies is
  exactly the divergence class that produced the R:R bug in generate_signals.
  There is now one exit policy, in one place.

Circuit-breaker detection, the one genuinely useful behaviour unique to the old
implementation, is preserved below and folded into the delegated run.
"""

from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, logger

MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def check_circuit_locks(sb, prices: dict[str, float]) -> list[str]:
    """
    Flag positions locked at an NSE circuit limit.

    A circuit-locked stock has a frozen last price. A stop check reads that as a
    live quote and concludes the stop is safe when in reality there is no bid.
    The old implementation approximated the circuit as +/-20% of ENTRY price,
    which is wrong twice over: circuits are set against the previous close, not
    your entry, and the band is 2/5/10/20% depending on the scrip. Kite's quote
    payload carries the real lower_circuit_limit / upper_circuit_limit, so use
    those.
    """
    from kite.kite_client import fetch_quotes
    alerts = []
    quotes = fetch_quotes(list(prices.keys()))
    for sym, q in quotes.items():
        ltp, lo, hi = q.get("ltp", 0), q.get("lower_circuit", 0), q.get("upper_circuit", 0)
        if not ltp:
            continue
        tol = ltp * 0.001
        if lo and abs(ltp - lo) <= tol:
            alerts.append(f"🔻 {sym} locked at LOWER circuit Rs.{ltp:.2f} — no exit liquidity")
        elif hi and abs(ltp - hi) <= tol:
            alerts.append(f"🔺 {sym} locked at UPPER circuit Rs.{ltp:.2f} — price frozen, SL check paused")
    return alerts


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — sl_monitor skipped")
        return {"status": "skipped"}

    if not is_market_open():
        logger.info("Market closed — sl_monitor skipping (outside 09:15-15:30 IST)")
        return {"status": "market_closed"}

    logger.info("sl_monitor -> delegating to position_lifecycle (single exit policy)")

    from control.position_lifecycle import main as lifecycle_main
    result = lifecycle_main(reconcile=False, manage=True)

    # Circuit-lock check runs on top of the delegated management pass.
    try:
        sb = get_supabase()
        rows = (sb.table("open_positions").select("symbol,current_price")
                  .eq("status", "ACTIVE").execute().data) or []
        prices = {r["symbol"]: float(r.get("current_price") or 0)
                  for r in rows if r.get("current_price")}
        if prices:
            locks = check_circuit_locks(sb, prices)
            if locks:
                from alerts.send_alerts import send_message
                send_message("<b>⚡ Circuit Locks</b>\n\n" + "\n".join(locks),
                             subject_suffix="Circuit Lock")
                for l in locks:
                    logger.warning(f"  {l}")
                result["circuit_locks"] = len(locks)
    except Exception as e:
        logger.warning(f"  Circuit-lock check failed (non-fatal): {e}")

    return result


if __name__ == "__main__":
    print(main())
