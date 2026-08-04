"""
TradeOS v7 — Phase 2: Position Target Monitor (SG6 + SG7)
Runs every 30 min during market hours alongside sl_monitor in pipeline_intraday.yml.

SG6 — TARGET PRICE MONITOR:
  Problem: no alert fires when a held position reaches its intended exit target.
  You must manually check P&L throughout the session.
  Fix: reads open_positions.target_price vs live Kite LTP. On hit:
    - Telegram alert: 🎯 TARGET HIT: SBIN ₹842 reached target ₹840 | P&L: +12.3%
    - Sets target_hit=True, target_hit_at=now() on open_positions
    - Writes data_anomalies INFO row for morning brief context
    - Does NOT auto-exit (Phase 2 supervised — you decide to exit or trail)

SG7 — TRAILING STOP-LOSS:
  Problem: fixed SL only. Once a position moves in your favour, the SL does not
  trail upward — profit is not protected.
  Fix: reads trailing_sl_pct + high_water_mark from open_positions. If LTP > HWM:
    - Updates high_water_mark = LTP
    - Recomputes active_sl = HWM * (1 - trailing_sl_pct/100)
    - Writes updated active_sl to open_positions
    - Telegram: 📈 TRAILING SL UPDATED: SBIN SL ₹798→₹812 (HWM ₹852, trail 4.7%)
    - sl_monitor.py then protects the new SL level

Requires: KITE_ACCESS_TOKEN in environment (set by kite_token_refresh.py daily).
Non-fatal if Kite unavailable — exits cleanly with a warning.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")


# ── Kite LTP fetch ────────────────────────────────────────────────────────

def get_live_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch live LTP from Kite for a list of NSE symbols.
    Returns {symbol: ltp}. Empty dict if Kite unavailable.
    """
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    api_key = os.getenv("KITE_API_KEY", "")
    if not token or not api_key:
        logger.warning("KITE_ACCESS_TOKEN or KITE_API_KEY not set — target monitor skipped")
        return {}
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)
        instruments = [f"NSE:{sym}" for sym in symbols]
        quotes = kite.ltp(instruments)
        return {
            sym: quotes.get(f"NSE:{sym}", {}).get("last_price", 0)
            for sym in symbols
        }
    except Exception as e:
        logger.warning(f"Kite LTP fetch failed (non-fatal): {e}")
        return {}


# ── SG6: Target monitor ───────────────────────────────────────────────────

def check_targets(sb, positions: list[dict], prices: dict[str, float]) -> list[str]:
    """
    Check each position against its target price.
    Returns list of alert messages for Telegram.
    """
    alerts = []
    today_str = str(today_ist())
    now_utc = datetime.now(timezone.utc).isoformat()

    for pos in positions:
        sym = pos.get("symbol") or ""
        target = pos.get("target_price")
        already_hit = pos.get("target_hit", False)

        if not target or already_hit or sym not in prices:
            continue

        ltp = prices[sym]
        entry = pos.get("entry_price") or 1
        pnl_pct = round((ltp - entry) / entry * 100, 2)

        if ltp >= float(target):
            if DRY_RUN:
                logger.info(f"[DRY RUN] TARGET HIT: {sym} LTP={ltp} >= target={target}")
                continue

            # Mark target hit in open_positions
            try:
                sb.table("open_positions").update({
                    "target_hit":    True,
                    "target_hit_at": now_utc,
                    "current_price": ltp,
                }).eq("symbol", sym).eq("status", "ACTIVE").execute()
            except Exception as e:
                logger.warning(f"target_hit update failed for {sym}: {e}")

            # Write data_anomalies INFO row (feeds AG3 morning brief if re-checked)
            try:
                sb.table("data_anomalies").insert({
                    "date":       today_str,
                    "check_name": "target_hit",
                    "severity":   "INFO",
                    "value":      str(ltp),
                    "message":    f"{sym} hit target ₹{target:.2f} at ₹{ltp:.2f} | P&L: {pnl_pct:+.1f}%",
                    "affected":   sym,
                }).execute()
            except Exception as e:
                logger.debug(f"data_anomalies target_hit write failed: {e}")

            sector = pos.get("sector", "")
            msg = (
                f"🎯 <b>TARGET HIT: {sym}</b>\n"
                f"  LTP: ₹{ltp:.2f} | Target: ₹{target:.2f}\n"
                f"  P&L: <b>{pnl_pct:+.1f}%</b>"
                + (f" | {sector}" if sector else "")
                + f"\n  <i>Action: review exit or activate trailing SL</i>"
            )
            alerts.append(msg)
            logger.success(f"TARGET HIT: {sym} @ ₹{ltp:.2f} (target ₹{target:.2f}, P&L {pnl_pct:+.1f}%)")

    return alerts


# ── SG7: Trailing SL ──────────────────────────────────────────────────────

def update_trailing_sl(sb, positions: list[dict], prices: dict[str, float]) -> list[str]:
    """
    Update trailing stop-loss for positions with trailing_sl_pct set.
    Returns list of alert messages for Telegram.
    """
    alerts = []
    for pos in positions:
        sym            = pos.get("symbol") or ""
        trail_pct      = pos.get("trailing_sl_pct")
        hwm            = pos.get("high_water_mark") or pos.get("entry_price") or 0
        current_sl     = pos.get("active_sl") or 0

        if not trail_pct or sym not in prices:
            continue

        ltp = prices[sym]
        trail_pct_f = float(trail_pct)
        hwm_f = float(hwm)

        # New high water mark?
        if ltp > hwm_f:
            new_hwm = ltp
            new_sl  = round(new_hwm * (1 - trail_pct_f / 100), 2)

            if DRY_RUN:
                logger.info(
                    f"[DRY RUN] TRAILING SL: {sym} HWM ₹{hwm_f:.2f}→₹{new_hwm:.2f} "
                    f"SL ₹{current_sl:.2f}→₹{new_sl:.2f}"
                )
                continue

            try:
                sb.table("open_positions").update({
                    "high_water_mark": new_hwm,
                    "active_sl":       new_sl,
                    "current_price":   ltp,
                }).eq("symbol", sym).eq("status", "ACTIVE").execute()
            except Exception as e:
                logger.warning(f"trailing SL update failed for {sym}: {e}")
                continue

            sl_move = new_sl - float(current_sl)
            msg = (
                f"📈 <b>TRAILING SL UPDATED: {sym}</b>\n"
                f"  SL: ₹{float(current_sl):.2f} → ₹{new_sl:.2f} ({sl_move:+.2f})\n"
                f"  High water mark: ₹{new_hwm:.2f} | Trail: {trail_pct_f:.1f}%"
            )
            alerts.append(msg)
            logger.info(
                f"TRAILING SL: {sym} SL ₹{current_sl:.2f}→₹{new_sl:.2f} "
                f"(HWM ₹{new_hwm:.2f}, trail {trail_pct_f:.1f}%)"
            )

    return alerts


# ── Telegram send ─────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Send Telegram message with retry (SG4 pattern)."""
    import time, requests as _req
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id or DRY_RUN:
        if DRY_RUN:
            logger.info(f"[DRY RUN] Telegram: {message[:80]}")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for attempt, wait in enumerate([0, 5, 15], start=1):
        try:
            if wait:
                time.sleep(wait)
            resp = _req.post(url, json={
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            }, timeout=10)
            if resp.status_code == 200:
                return
            logger.warning(f"Telegram attempt {attempt} HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Telegram attempt {attempt} failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — position_target_monitor skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Position Target Monitor starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb = get_supabase()

    # Load all active positions
    positions = (sb.table("open_positions")
                   .select("symbol,entry_price,current_price,target_price,target_pct,"
                           "target_hit,trailing_sl_pct,high_water_mark,active_sl,sector,status")
                   .eq("status", "ACTIVE")
                   .execute().data or [])

    if not positions:
        logger.info("No active positions — nothing to monitor")
        return {"status": "ok", "positions": 0}

    # Only fetch prices for positions that have target or trailing SL set
    relevant = [
        p for p in positions
        if (p.get("target_price") and not p.get("target_hit"))
        or p.get("trailing_sl_pct")
    ]

    if not relevant:
        logger.info(f"{len(positions)} positions active, none with target_price or trailing_sl_pct set")
        return {"status": "ok", "positions": len(positions), "relevant": 0}

    symbols = [p["symbol"] for p in relevant if p.get("symbol")]
    logger.info(f"Checking {len(symbols)} positions for target/trailing SL")

    prices = get_live_prices(symbols)
    if not prices:
        logger.warning("No prices fetched — Kite unavailable. Skipping target/SL checks.")
        return {"status": "ok", "positions": len(positions), "prices_fetched": 0}

    all_alerts: list[str] = []

    # SG6: target price checks
    target_alerts = check_targets(sb, relevant, prices)
    all_alerts.extend(target_alerts)

    # SG7: trailing SL updates
    trail_alerts = update_trailing_sl(sb, relevant, prices)
    all_alerts.extend(trail_alerts)

    # Send combined alert if any
    for alert in all_alerts:
        send_telegram(alert)

    logger.success(
        f"Position Target Monitor done: {len(prices)} prices | "
        f"{len(target_alerts)} target hits | {len(trail_alerts)} SL updates"
    )
    return {
        "status":       "ok",
        "positions":    len(positions),
        "prices":       len(prices),
        "target_hits":  len(target_alerts),
        "sl_updates":   len(trail_alerts),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v7 — Position Target Monitor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())
