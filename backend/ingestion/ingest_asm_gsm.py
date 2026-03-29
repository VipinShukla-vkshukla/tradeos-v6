"""
TradeOS v6 — Phase 1/2: ASM/GSM + F&O Ban Safety Lists
Fetches NSE surveillance lists daily. These are HARD BLOCKERS —
no BUY signal fires on any stock in these lists.

Data retention: REPLACE daily (full refresh, stale entries removed)

Wire in run_pipeline.py as step 12_asm_gsm (Phase 2+, non-fatal):
    def step_asm_gsm():
        from ingestion.ingest_asm_gsm import main as fn; return fn()

Table schema (safety_lists — per-symbol rows):
    symbol      TEXT NOT NULL
    list_type   TEXT NOT NULL  -- 'ASM' | 'GSM' | 'FO_BAN'
    stage       TEXT           -- ASM/GSM stage number, NULL for FO_BAN
    listed_date DATE           -- date NSE added the symbol to the surveillance list
    ingested_at TIMESTAMPTZ    -- timestamp when this record was inserted into Supabase

Also updates stock_data_daily flags:
    asm_flag    BOOLEAN  -- True for ASM + GSM symbols
    fo_ban_flag BOOLEAN  -- True for F&O ban symbols
"""
import sys
import os
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# ── Correct NSE API URLs ───────────────────────────────────────────────────────
# ASM/GSM: JSON APIs confirmed from browser Network tab — require warmed-up session.
# F&O Ban: plain CSV from nsearchives — no session/cookie needed, direct fetch.
NSE_ASM_URL    = "https://www.nseindia.com/api/reportASM"
NSE_GSM_URL    = "https://www.nseindia.com/api/reportGSM"
NSE_FO_BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"

# Referer pages for session warmup (ASM/GSM only — F&O ban is a direct CSV)
NSE_HOMEPAGE = "https://www.nseindia.com"
NSE_ASM_PAGE = "https://www.nseindia.com/reports/asm"
NSE_GSM_PAGE = "https://www.nseindia.com/reports/gsm"

# Minimal headers that NSE accepts — overly complex browser headers can
# trigger Akamai bot detection. Keep it simple: User-Agent + Accept only.
NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}


def nse_session() -> requests.Session:
    """
    Build a warmed-up NSE session with cookies.
    Pattern: GET homepage first to receive session cookies, then pass those
    cookies into all subsequent API calls. NSE returns empty body (not 401)
    when cookies are missing — hence the confusing JSON parse error.
    """
    session = requests.Session()
    try:
        resp = session.get(NSE_HOMEPAGE, headers=NSE_HEADERS, timeout=15)
        # Explicitly carry the cookies forward on all future requests
        session.cookies.update(resp.cookies)
        time.sleep(2)
    except Exception as e:
        logger.warning(f"NSE session warmup failed (continuing): {e}")
    return session


def _fetch_nse(session: requests.Session, url: str, referer: str,
               label: str) -> list | dict | None:
    """
    Shared fetch helper. Updates Referer header before each call.
    Returns parsed JSON or None on failure.
    """
    try:
        headers = {**NSE_HEADERS, "Referer": referer}
        resp = session.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        if not resp.content:
            logger.warning(f"{label}: empty response from NSE (session may have expired)")
            return None
        return resp.json()
    except requests.HTTPError as e:
        logger.warning(f"{label} HTTP error {e.response.status_code}: {e}")
    except Exception as e:
        logger.warning(f"{label} fetch failed: {e}")
    return None


def _now_utc() -> str:
    """Return current UTC timestamp as ISO 8601 string for ingested_at."""
    return datetime.now(timezone.utc).isoformat()


def fetch_asm(session: requests.Session) -> list[dict]:
    data = _fetch_nse(session, NSE_ASM_URL, NSE_ASM_PAGE, "ASM")
    if data is None:
        return []
    rows = []
    ingested_at = _now_utc()
    # ASM JSON structure: {"longterm": {"data": [...]}, "shortterm": {"data": [...]}}
    # Collect items from all sub-keys (longterm, shortterm, or any future additions)
    if isinstance(data, dict):
        items = []
        for key, val in data.items():
            if isinstance(val, dict) and "data" in val:
                items.extend(val["data"])   # e.g. data["longterm"]["data"]
            elif isinstance(val, list):
                items.extend(val)           # flat list fallback
    else:
        items = data if isinstance(data, list) else []

    for item in items:
        sym = str(item.get("symbol", "") or item.get("Symbol", "") or "").strip().upper()
        if sym:
            raw_date = item.get("asmTime", "") or ""
            try:
                # asmTime format: "27-Mar-2026" (date only) or "27-Mar-2026 HH:MM:SS"
                listed_date = datetime.strptime(raw_date.split()[0], "%d-%b-%Y").date().isoformat()
            except (ValueError, IndexError):
                listed_date = today_ist().isoformat()
            rows.append({
                "symbol":      sym,
                "list_type":   "ASM",
                "stage":       str(item.get("asmSurvIndicator", "") or item.get("asmStage", "") or item.get("stage", "") or ""),
                "listed_date": listed_date,
                "ingested_at": ingested_at,
            })
    return rows


def fetch_gsm(session: requests.Session) -> list[dict]:
    data = _fetch_nse(session, NSE_GSM_URL, NSE_GSM_PAGE, "GSM")
    if data is None:
        return []
    rows = []
    ingested_at = _now_utc()
    items = data.get("data", data) if isinstance(data, dict) else data
    for item in (items if isinstance(items, list) else []):
        sym = str(item.get("symbol", "") or item.get("Symbol", "") or "").strip().upper()
        if sym:
            raw_date = item.get("gsmTime", "") or ""
            try:
                # gsmTime format: "27-Mar-2026 08:07:02"
                listed_date = datetime.strptime(raw_date.split()[0], "%d-%b-%Y").date().isoformat()
            except (ValueError, IndexError):
                listed_date = today_ist().isoformat()
            rows.append({
                "symbol":      sym,
                "list_type":   "GSM",
                "stage":       str(item.get("gsmStage", "") or item.get("stage", "") or ""),
                "listed_date": listed_date,
                "ingested_at": ingested_at,
            })
    return rows


def fetch_fo_ban() -> list[dict]:
    """
    F&O ban list CSV from nsearchives. No session/cookies needed.
    Actual format (confirmed from downloaded file):
      Line 1: "Securities in Ban For Trade Date DD-MON-YYYY:"  ← header, skip
      Line 2+: "1,SAIL"  ← serial number, symbol (comma-separated)
    nsearchives can be slow — use separate connect/read timeouts.
    """
    try:
        resp = requests.get(
            NSE_FO_BAN_URL,
            timeout=(10, 45),   # (connect timeout, read timeout)
            headers={"User-Agent": NSE_HEADERS["User-Agent"]},
        )
        resp.raise_for_status()
        rows = []
        listed_date = today_ist().isoformat()   # fallback
        ingested_at = _now_utc()
        for line in resp.text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Header line: "Securities in Ban For Trade Date 30-MAR-2026:"
            if line.lower().startswith("securities"):
                try:
                    raw_date = line.rstrip(":").split()[-1]   # "30-MAR-2026"
                    listed_date = datetime.strptime(raw_date, "%d-%b-%Y").date().isoformat()
                except (ValueError, IndexError):
                    pass
                continue
            parts = line.split(",")
            if len(parts) < 2:
                continue
            sym = parts[1].strip().upper()   # format: "1,SYMBOL"
            if sym:
                rows.append({
                    "symbol":      sym,
                    "list_type":   "FO_BAN",
                    "stage":       None,
                    "listed_date": listed_date,
                    "ingested_at": ingested_at,
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
    sb    = get_supabase()
    today = today_ist().isoformat()

    session  = nse_session()
    asm_rows = fetch_asm(session)
    gsm_rows = fetch_gsm(session)
    ban_rows = fetch_fo_ban()
    all_rows = asm_rows + gsm_rows + ban_rows

    logger.info(f"ASM: {len(asm_rows)} | GSM: {len(gsm_rows)} | F&O Ban: {len(ban_rows)}")

    if not all_rows:
        logger.warning("No safety list data fetched — NSE may be unavailable, keeping existing data")
        return {"status": "no_data"}

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(all_rows)} safety list rows")
        for r in all_rows[:5]:
            logger.info(f"  {r['list_type']}: {r['symbol']} (stage={r['stage']}, listed_date={r['listed_date']}, ingested_at={r['ingested_at']})")
        return {"status": "dry_run", "asm": len(asm_rows), "gsm": len(gsm_rows), "fo_ban": len(ban_rows)}

    try:
        # REPLACE: full DELETE then bulk INSERT (daily refresh)
        sb.table("safety_lists").delete().neq("symbol", "__never__").execute()
        for i in range(0, len(all_rows), 200):
            sb.table("safety_lists").insert(all_rows[i:i+200]).execute()

        # Also mirror flags into stock_data_daily
        asm_syms = {r["symbol"] for r in asm_rows + gsm_rows}
        ban_syms = {r["symbol"] for r in ban_rows}
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