"""
TradeOS v6 — Phase 1: FII/DII Flow Ingestion
Fetches daily FII/DII provisional data from NSE.
Published by NSE by ~7 PM daily.

Data retention: APPEND (upsert on date — one row per trading day)
"""
import sys
import time
import requests
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg_float, today_ist, is_kill_switch_active, logger

NSE_FII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

def fetch_fii_dii_from_nse(session: requests.Session, trade_date: date) -> dict | None:
    """Fetch FII/DII data from NSE for a given date."""
    date_str = trade_date.strftime("%d-%m-%Y")
    url = f"{NSE_FII_URL}?startDate={date_str}&endDate={date_str}"
    try:
        resp = session.get(url, headers=NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        # NSE returns list; find equity row
        for row in data:
            if row.get("category", "").upper() in ("FII/FPI", "FII"):
                fii_buy  = float(row.get("buyValue", 0) or 0)
                fii_sell = float(row.get("sellValue", 0) or 0)
                fii_net  = float(row.get("netValue", 0) or 0)
                break
        else:
            # Try first row
            row = data[0]
            fii_buy  = float(row.get("buyValue", 0) or 0)
            fii_sell = float(row.get("sellValue", 0) or 0)
            fii_net  = fii_buy - fii_sell

        # Find DII row
        dii_buy = dii_sell = dii_net = 0.0
        for row in data:
            if row.get("category", "").upper() == "DII":
                dii_buy  = float(row.get("buyValue", 0) or 0)
                dii_sell = float(row.get("sellValue", 0) or 0)
                dii_net  = float(row.get("netValue", 0) or 0)
                break

        return {
            "date":               trade_date.isoformat(),
            "fii_gross_buy_cr":   fii_buy,
            "fii_gross_sell_cr":  fii_sell,
            "fii_net_cr":         fii_net,
            "dii_gross_buy_cr":   dii_buy,
            "dii_gross_sell_cr":  dii_sell,
            "dii_net_cr":         dii_net,
        }
    except Exception as e:
        logger.warning(f"FII/DII fetch failed for {trade_date}: {e}")
        return None

def compute_rolling_flows(sb, today: date) -> dict:
    """Compute 5D, 10D, 20D rolling FII net flows and signal."""
    try:
        rows = sb.table("fii_dii_flow").select("date,fii_net_cr") \
            .order("date", desc=True).limit(20).execute().data
        nets = [float(r["fii_net_cr"] or 0) for r in rows]
        n5  = sum(nets[:5])  if len(nets) >= 5  else None
        n10 = sum(nets[:10]) if len(nets) >= 10 else None
        n20 = sum(nets[:20]) if len(nets) >= 20 else None

        caution_thresh     = cfg_float("fii_caution_threshold",     -2000)
        accelerator_thresh = cfg_float("fii_accelerator_threshold",  1000)

        # Signal logic
        signal = "NEUTRAL"
        if n5 is not None:
            consecutive_sell = sum(1 for n in nets[:5] if n < caution_thresh / 5)
            if consecutive_sell >= 4:
                signal = "CAUTION"
            elif n5 > accelerator_thresh * 3:
                signal = "ACCELERATOR"

        return {
            "fii_net_5d_cumulative":  round(n5,  2) if n5  is not None else None,
            "fii_net_10d_cumulative": round(n10, 2) if n10 is not None else None,
            "fii_net_20d_cumulative": round(n20, 2) if n20 is not None else None,
            "fii_signal":             signal,
        }
    except Exception as e:
        logger.warning(f"Rolling flow compute failed: {e}")
        return {"fii_net_5d_cumulative": None, "fii_net_10d_cumulative": None,
                "fii_net_20d_cumulative": None, "fii_signal": "NEUTRAL"}

def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — FII/DII ingestion skipped")
        return {"status": "skipped", "reason": "kill_switch"}
    logger.info("FII/DII Ingestion starting")
    sb = get_supabase()
    today = today_ist()

    session = requests.Session()
    # Warm up session cookie
    try:
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        time.sleep(1)
    except Exception:
        pass

    row = fetch_fii_dii_from_nse(session, today)
    if not row:
        logger.warning(f"No FII/DII data for {today} — market may be closed or NSE unavailable")
        return {"status": "no_data", "date": today.isoformat()}

    rolling = compute_rolling_flows(sb, today)
    row.update(rolling)

    # APPEND: upsert on date (one row per trading day, never delete)
    sb.table("fii_dii_flow").upsert(row, on_conflict="date").execute()
    logger.success(f"FII/DII saved: net={row['fii_net_cr']:+.0f}Cr, signal={row.get('fii_signal','?')}")
    return row

if __name__ == "__main__":
    main()
