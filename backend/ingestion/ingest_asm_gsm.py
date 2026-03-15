"""
TradeOS v6 — Phase 1/2: ASM/GSM + F&O Ban Safety Lists
Fetches NSE surveillance lists daily. These are HARD BLOCKERS —
no BUY signal fires on any stock in these lists.

Data retention: REPLACE daily (full refresh, stale entries removed)

Wire in run_pipeline.py as step 08a_asm_gsm (Phase 2+, non-fatal):
    def step_asm_gsm():
        from ingestion.ingest_asm_gsm import main as fn; return fn()

Table schema (safety_lists — per-symbol rows):
    symbol      TEXT NOT NULL
    list_type   TEXT NOT NULL  -- 'ASM' | 'GSM' | 'FO_BAN'
    stage       TEXT           -- ASM/GSM stage number, NULL for FO_BAN
    added_date  DATE

Also updates stock_data_daily flags:
    asm_flag    BOOLEAN  -- True for ASM + GSM symbols
    fo_ban_flag BOOLEAN  -- True for F&O ban symbols
"""
import sys
import os
import time
import requests
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept":     "application/json, text/plain, */*",
    "Referer":    "https://www.nseindia.com/",
}

NSE_ASM_URL    = "https://www.nseindia.com/api/reportsmf?index=asmSecurities"
NSE_GSM_URL    = "https://www.nseindia.com/api/reportsmf?index=gsmSecurities"
NSE_FO_BAN_URL = "https://www.nseindia.com/api/fo-ban-list"


def nse_session() -> requests.Session:
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        time.sleep(1)
    except Exception:
        pass
    return session


def fetch_asm(session: requests.Session) -> list[dict]:
    try:
        resp = session.get(NSE_ASM_URL, headers=NSE_HEADERS, timeout=15)
        data = resp.json()
        rows = []
        for item in (data.get("data", []) or data if isinstance(data, list) else []):
            sym = str(item.get("symbol", "") or item.get("Symbol", "") or "").strip().upper()
            if sym:
                rows.append({
                    "symbol":     sym,
                    "list_type":  "ASM",
                    "stage":      str(item.get("asmStage", "") or item.get("stage", "") or ""),
                    "added_date": today_ist().isoformat(),
                })
        return rows
    except Exception as e:
        logger.warning(f"ASM fetch failed: {e}")
        return []


def fetch_gsm(session: requests.Session) -> list[dict]:
    try:
        resp = session.get(NSE_GSM_URL, headers=NSE_HEADERS, timeout=15)
        data = resp.json()
        rows = []
        for item in (data.get("data", []) or data if isinstance(data, list) else []):
            sym = str(item.get("symbol", "") or "").strip().upper()
            if sym:
                rows.append({
                    "symbol":     sym,
                    "list_type":  "GSM",
                    "stage":      str(item.get("gsmStage", "") or ""),
                    "added_date": today_ist().isoformat(),
                })
        return rows
    except Exception as e:
        logger.warning(f"GSM fetch failed: {e}")
        return []


def fetch_fo_ban(session: requests.Session) -> list[dict]:
    try:
        resp = session.get(NSE_FO_BAN_URL, headers=NSE_HEADERS, timeout=15)
        data = resp.json()
        rows = []
        banned = data.get("data", data) if isinstance(data, dict) else data
        for item in (banned if isinstance(banned, list) else []):
            sym = str(item.get("symbol", "") or item.get("Symbol", "") or "").strip().upper()
            if sym:
                rows.append({
                    "symbol":     sym,
                    "list_type":  "FO_BAN",
                    "stage":      None,
                    "added_date": today_ist().isoformat(),
                })
        return rows
    except Exception as e:
        logger.warning(f"F&O ban fetch failed: {e}")
        return []


def update_stock_data_flags(sb, asm_syms: set[str], ban_syms: set[str], today: str):
    """
    Mirror ASM/GSM/FO_BAN flags into stock_data_daily for today.
    Non-fatal — if stock_data_daily doesn't have the columns yet, skip.
    """
    try:
        for sym in asm_syms:
            sb.table("stock_data_daily").update({"asm_flag": True}) \
              .eq("date", today).eq("symbol", sym).execute()
        for sym in ban_syms:
            sb.table("stock_data_daily").update({"fo_ban_flag": True}) \
              .eq("date", today).eq("symbol", sym).execute()
        logger.info(f"stock_data_daily flags: {len(asm_syms)} asm_flag, {len(ban_syms)} fo_ban_flag")
    except Exception as e:
        logger.warning(f"stock_data_daily flag update skipped (non-fatal): {e}")


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — ingest_asm_gsm skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Safety Lists Ingestion starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb = get_supabase()
    today = today_ist().isoformat()

    session = nse_session()

    asm_rows = fetch_asm(session)
    gsm_rows = fetch_gsm(session)
    ban_rows = fetch_fo_ban(session)
    all_rows = asm_rows + gsm_rows + ban_rows

    logger.info(f"ASM: {len(asm_rows)} | GSM: {len(gsm_rows)} | F&O Ban: {len(ban_rows)}")

    if not all_rows:
        logger.warning("No safety list data fetched — NSE may be unavailable, keeping existing data")
        return {"status": "no_data"}

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(all_rows)} safety list rows")
        for r in all_rows[:5]:
            logger.info(f"  {r['list_type']}: {r['symbol']} (stage={r['stage']})")
        return {"status": "dry_run", "asm": len(asm_rows), "gsm": len(gsm_rows), "fo_ban": len(ban_rows)}

    try:
        # REPLACE: full DELETE then bulk INSERT (daily refresh)
        sb.table("safety_lists").delete().neq("symbol", "__never__").execute()
        for i in range(0, len(all_rows), 200):
            sb.table("safety_lists").insert(all_rows[i:i+200]).execute()

        # Also mirror flags into stock_data_daily
        asm_syms  = {r["symbol"] for r in asm_rows + gsm_rows}
        ban_syms  = {r["symbol"] for r in ban_rows}
        update_stock_data_flags(sb, asm_syms, ban_syms, today)

        logger.success(f"Safety lists saved: {len(all_rows)} total entries")
        return {"status": "ok", "asm": len(asm_rows), "gsm": len(gsm_rows), "fo_ban": len(ban_rows)}

    except Exception as e:
        logger.error(f"Failed to save safety lists: {e}")
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    result = main()
    print(result)
