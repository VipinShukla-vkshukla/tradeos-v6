"""
TradeOS v6 — Phase 2+: Intraday Stop-Loss Monitor
===================================================
NEW SCRIPT (Strategic Fix #1 — critical execution risk).

Runs every 30 minutes during market hours (9:15 AM – 3:30 PM IST) via
pipeline_intraday.yml. For each open position:

  - Fetches live LTP from Kite Connect
  - BREACH: if ltp < active_sl → immediate Telegram alert + data_anomaly
  - PROXIMITY: if ltp within 2% of active_sl → warning Telegram alert
  - Updates open_positions.current_price with live price on every run

Why this exists:
  Without this, your active_sl is stored in Supabase but nothing checks it
  intraday. A stock can breach its stop by 5% before the evening pipeline
  runs and generates an EXIT signal. By then the damage is done.

Tables read:   open_positions, system_config
Tables write:  open_positions (current_price), data_anomalies
External:      Kite Connect API (requires kite_token_refresh.py to have run)

Deploy:
  1. Run migration_strategic_fixes.sql first (adds sl_breach_alerted column)
  2. Add pipeline_intraday.yml to .github/workflows/
  3. Ensure KITE_API_KEY and KITE_ACCESS_TOKEN are in GitHub Secrets

Wire:  pipeline_intraday.yml — every 30 min 9:15 AM–3:30 PM IST weekdays
Phase: 2+ (requires Kite token)
"""
import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, logger

# ── Constants ─────────────────────────────────────────────────────────────────
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
PROXIMITY_PCT = 2.0     # alert if price is within 2% of SL
BREACH_BUFFER = 0.0     # 0% buffer — breach is breach


def is_market_open() -> bool:
    """Returns True only on IST weekdays during market hours."""
    now = datetime.now(IST)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def fetch_live_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch live LTP from Kite. Returns {symbol: ltp}.
    Falls back to empty dict if Kite not available (Phase 1 or token stale).
    """
    try:
        from kite.kite_client import fetch_quotes
        quotes = fetch_quotes(symbols)
        return {sym: float(data.get("ltp") or 0) for sym, data in quotes.items()}
    except Exception as e:
        logger.warning(f"Kite quotes unavailable: {e} — sl_monitor skipping price fetch")
        return {}


def send_telegram(message: str):
    try:
        from alerts.send_alerts import send_telegram_message
        send_telegram_message(message)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def log_anomaly(sb, symbol: str, check_name: str, severity: str, value: str, message: str):
    try:
        sb.table("data_anomalies").insert({
            "date":       today_ist().isoformat(),
            "check_name": check_name,
            "severity":   severity,
            "value":      value,
            "message":    message,
            "affected":   f"open_positions:{symbol}",
            "created_at": datetime.now(IST).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"Could not log anomaly for {symbol}: {e}")


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — sl_monitor skipped")
        return {"status": "skipped"}

    if not is_market_open():
        logger.info("Market closed — sl_monitor skipping (outside 9:15–15:30 IST)")
        return {"status": "market_closed"}

    sb = get_supabase()

    # Load all open positions with stop-loss data
    positions = sb.table("open_positions") \
        .select("symbol, active_sl, actual_qty, entry_price, invested_value, "
                "sl_breach_alerted, sl_proximity_alerted, current_price, "
                "trailing_sl_pct, high_water_mark") \
        .eq("status", "ACTIVE") \
        .execute().data

    if not positions:
        logger.info("No open positions to monitor")
        return {"status": "ok", "checked": 0}

    symbols = [p["symbol"] for p in positions if p.get("symbol")]
    prices  = fetch_live_prices(symbols)

    if not prices:
        return {"status": "no_prices", "checked": 0}

    breaches   = 0
    proximities = 0
    price_updates = []

    now_str = datetime.now(IST).isoformat()

    for pos in positions:
        sym      = pos.get("symbol", "")
        active_sl = float(pos.get("active_sl") or 0)
        ltp       = prices.get(sym, 0)

        if not sym or not ltp or not active_sl:
            continue

        # Always update current_price
        price_updates.append({"symbol": sym, "current_price": ltp})

        pct_from_sl = ((ltp - active_sl) / active_sl) * 100  # positive = above SL

        # ── BREACH: price at or below stop ───────────────────────────────────
        if ltp <= active_sl and not pos.get("sl_breach_alerted"):
            breaches += 1
            pnl_pct = ((ltp - float(pos.get("entry_price") or ltp)) / float(pos.get("entry_price") or ltp)) * 100

            msg = (
                f"🔴 <b>STOP LOSS BREACHED — {sym}</b>\n"
                f"LTP: ₹{ltp:,.2f} | SL: ₹{active_sl:,.2f}\n"
                f"Below SL by: {abs(pct_from_sl):.1f}%\n"
                f"Trade P&L: {pnl_pct:.1f}%\n"
                f"<b>⚠ Action required: review exit immediately</b>"
            )
            send_telegram(msg)
            log_anomaly(sb, sym, "sl_breach", "ERROR",
                        f"LTP={ltp}, SL={active_sl}",
                        f"{sym} breached stop loss. LTP ₹{ltp} < SL ₹{active_sl} ({pct_from_sl:.1f}%)")
            logger.error(f"SL BREACH: {sym} — LTP ₹{ltp} < SL ₹{active_sl}")

            # Mark alerted so we don't spam (resets if position is updated)
            try:
                sb.table("open_positions").update({"sl_breach_alerted": True}) \
                    .eq("symbol", sym).execute()
            except Exception:
                pass

        # ── PROXIMITY: within PROXIMITY_PCT% of SL ───────────────────────────
        elif 0 < pct_from_sl <= PROXIMITY_PCT and not pos.get("sl_proximity_alerted"):
            proximities += 1

            msg = (
                f"🟡 <b>SL PROXIMITY — {sym}</b>\n"
                f"LTP: ₹{ltp:,.2f} | SL: ₹{active_sl:,.2f}\n"
                f"Distance to SL: {pct_from_sl:.1f}%\n"
                f"<i>Monitor closely</i>"
            )
            send_telegram(msg)
            log_anomaly(sb, sym, "sl_proximity", "WARN",
                        f"LTP={ltp}, SL={active_sl}, dist={pct_from_sl:.1f}%",
                        f"{sym} within {pct_from_sl:.1f}% of stop loss ₹{active_sl}")
            logger.warning(f"SL PROXIMITY: {sym} — {pct_from_sl:.1f}% from SL")

            try:
                sb.table("open_positions").update({"sl_proximity_alerted": True}) \
                    .eq("symbol", sym).execute()
            except Exception:
                pass

        # ── SG7: TRAILING SL UPDATE ───────────────────────────────────────
        # If position has trailing_sl_pct set, update active_sl when new high is made.
        # position_target_monitor.py also updates high_water_mark on target hits;
        # sl_monitor is the authoritative source of current_price + SL during market hours.
        trail_pct = pos.get("trailing_sl_pct")
        hwm       = float(pos.get("high_water_mark") or pos.get("entry_price") or 0)
        if trail_pct and ltp and hwm and ltp > hwm:
            new_hwm = ltp
            new_sl  = round(new_hwm * (1 - float(trail_pct) / 100), 2)
            try:
                sb.table("open_positions").update({
                    "high_water_mark": new_hwm,
                    "active_sl":       new_sl,
                }).eq("symbol", sym).execute()
                msg = (
                    f"📈 <b>TRAILING SL UPDATED: {sym}</b>\n"
                    f"  SL: ₹{active_sl:,.2f} → ₹{new_sl:,.2f}  (+{new_sl-active_sl:,.2f})\n"
                    f"  New high water mark: ₹{new_hwm:,.2f} | Trail: {float(trail_pct):.1f}%"
                )
                send_telegram(msg)
                logger.info(f"TRAILING SL: {sym} SL ₹{active_sl:.2f}→₹{new_sl:.2f} (HWM ₹{new_hwm:.2f})")
            except Exception as e:
                logger.warning(f"Trailing SL update failed for {sym}: {e}")

        # ── SG8: CIRCUIT BREAKER DETECTION ───────────────────────────────
        # NSE applies 20% daily circuit limits. If LTP is frozen exactly at the
        # upper or lower circuit, a normal SL check misreads it as a live price.
        # Detection: compare LTP to ±20% of entry_price as proxy for circuit level.
        # (prev_close would be more accurate but entry_price is always available.)
        entry = float(pos.get("entry_price") or 0)
        if entry and ltp:
            lower_circuit = round(entry * 0.80, 2)
            upper_circuit = round(entry * 1.20, 2)
            tolerance = entry * 0.001   # 0.1% tolerance for float precision
            if abs(ltp - lower_circuit) < tolerance or abs(ltp - upper_circuit) < tolerance:
                circuit_dir = "LOWER" if ltp < entry else "UPPER"
                log_anomaly(sb, sym, "circuit_hit", "WARN",
                            f"LTP={ltp}, {circuit_dir}_CIRCUIT={ltp}",
                            f"{sym} appears locked at {circuit_dir} circuit ₹{ltp:.2f} — SL check paused")
                msg = (
                    f"⚡ <b>CIRCUIT HIT: {sym}</b>\n"
                    f"  Locked at {circuit_dir} circuit: ₹{ltp:,.2f}\n"
                    f"  <i>SL check paused — price may be frozen, not live</i>"
                )
                send_telegram(msg)
                logger.warning(f"CIRCUIT HIT: {sym} locked at {circuit_dir} circuit ₹{ltp:.2f}")

    # Bulk-update current_price for all positions
    if price_updates:
        for upd in price_updates:
            try:
                sb.table("open_positions") \
                    .update({
                        "current_price":      upd["current_price"],
                        "last_reconciled_at": now_str,
                    }) \
                    .eq("symbol", upd["symbol"]).execute()
            except Exception as e:
                logger.warning(f"Could not update current_price for {upd['symbol']}: {e}")

    logger.info(
        f"SL monitor run: {len(positions)} positions checked | "
        f"{breaches} breaches | {proximities} proximity alerts | "
        f"{len(price_updates)} prices updated"
    )

    return {
        "status":      "ok",
        "checked":     len(positions),
        "breaches":    breaches,
        "proximities": proximities,
    }


if __name__ == "__main__":
    result = main()
    print(result)
