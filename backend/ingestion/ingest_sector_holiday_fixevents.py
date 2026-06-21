"""
TradeOS v6 — Google Sheets Ingestion
Reads all 15 Sheet tabs → Supabase
Handles exact row offsets per tab (headers are NOT always row 1)
"""
import sys, re, json, math
from pathlib import Path
from datetime import date, datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from loguru import logger
import requests
from dateutil import parser as dateparser

from config import (
    get_supabase, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS,
    DEFAULT_STRATEGY_PARAMS, DRY_RUN, today_ist
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# ── Sheet tab definitions: name → (header_row, data_start_row) ─
# Row numbers are 1-indexed as in Google Sheets
SHEET_TABS = {
}

# ── Google Sheets client ─────────────────────────────────────
def get_sheets_service():
    creds = Credentials.from_service_account_file(
        str(Path(__file__).parent.parent / GOOGLE_CREDENTIALS),
        scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def read_tab(service, tab_name: str, max_rows: int = 2000) -> list[list]:
    """Read raw values from a Sheet tab."""
    range_str = f"'{tab_name}'!A1:BZ{max_rows}"
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=range_str,
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()
    return result.get("values", [])

# ── Parsers ──────────────────────────────────────────────────
def _scalar(v):
    if isinstance(v, pd.Series):
        return v.iloc[0] if not v.empty else None
    return v

def parse_date(v) -> date | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            from datetime import timedelta
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=int(v))).date()
        except Exception:
            return None
    s = str(v).strip()
    m = re.match(r"(\d+)\w*\s+(\w+)'(\d+)", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} 20{m.group(3)}", "%d %b %Y").date()
        except Exception:
            pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.split(" ")[0]).date()
    except Exception:
        pass
    return None

def parse_bool(v) -> bool | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return True
    if s in ("N", "NO", "FALSE", "0"):
        return False
    return None

def parse_num(v) -> float | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return None

def safe_str(v) -> str | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    s = str(v).strip()
    return None if s == "" else s

def safe_str(v) -> str | None:
    if v is None or v == "":
        return None
    s = str(v).strip()
    return None if s == "" else s

def rows_to_df(raw: list[list], header_row: int, data_start: int) -> pd.DataFrame:
    """Convert raw Sheet values to DataFrame using exact row offsets (1-indexed)."""
    if not raw or len(raw) < header_row:
        return pd.DataFrame()
    headers = raw[header_row - 1]
    # Normalize header names
    clean_headers = []
    seen_headers = {}
    for h in headers:
        s = str(h).strip()
        s = re.sub(r"^[^\w]+", "", s)
        s = re.sub(r"\s+", "_", s).lower()
        s = re.sub(r"[^\w]", "", s)
        s = s or f"col_{len(clean_headers)}"
        # Deduplicate: if column name already seen, append _2, _3, etc.
        if s in seen_headers:
            seen_headers[s] += 1
            s = f"{s}_{seen_headers[s]}"
        else:
            seen_headers[s] = 1
        clean_headers.append(s)
    data = raw[data_start - 1:]
    # Pad short rows
    rows = []
    for row in data:
        padded = list(row) + [None] * (len(clean_headers) - len(row))
        rows.append(padded[:len(clean_headers)])
    return pd.DataFrame(rows, columns=clean_headers)


# ── Tab-specific ingestors ────────────────────────────────────
    
    
def find_signal_date(sb, symbol: str, entry_date: str) -> str | None:
    """
    Auto-detect signal_date from signal_log.
    Looks for BUY_CANDIDATE/WATCH signal within 5 days before entry_date.
    """
    try:
        from datetime import date, timedelta
        entry    = date.fromisoformat(str(entry_date)[:10])
        lookback = (entry - timedelta(days=5)).isoformat()
        rows = (sb.table("signal_log")
                  .select("date")
                  .eq("symbol", symbol)
                  .in_("signal_type", ["BUY_CANDIDATE", "WATCH"])
                  .gte("date", lookback)
                  .lte("date", str(entry))
                  .order("date", desc=True)
                  .limit(1).execute().data)
        return rows[0]["date"] if rows else None
    except Exception:
        return None 


def compute_regime_score(
    breadth_pct:        float | None,
    india_vix:          float | None,
    nifty_vs_50dma_pct: float | None,
    weekly_rsi:         float | None,
) -> float:
    """
    Deterministic 0.0–1.0 score representing regime strength.
    Weights: breadth 40% | VIX 30% | price vs 50DMA 20% | RSI 10%
 
    Thresholds for regime label (used in ingest_market_regime):
        score >= 0.65  → RISK_ON
        score >= 0.35  → NEUTRAL
        score <  0.35  → RISK_OFF
 
    NOTE: regime *label* still comes from the Google Sheet (human-reviewed).
    regime_score is the numeric confidence behind that label — used by AI
    to distinguish "borderline RISK_OFF (0.32)" from "deep RISK_OFF (0.05)".
    """
    score = 0.0
 
    # Breadth — 40% weight (most important: is the market move broad?)
    if breadth_pct is not None:
        if breadth_pct > 60:    score += 0.40
        elif breadth_pct > 40:  score += 0.20
        # else 0
 
    # VIX — 30% weight (fear gauge)
    if india_vix is not None:
        if india_vix < 14:      score += 0.30
        elif india_vix < 20:    score += 0.15
        # else 0 (VIX >= 20 = elevated fear)
 
    # Price vs 50DMA — 20% weight
    if nifty_vs_50dma_pct is not None:
        if nifty_vs_50dma_pct > 2:      score += 0.20
        elif nifty_vs_50dma_pct > -2:   score += 0.10
        # else 0 (price well below 50DMA)
 
    # Weekly RSI — 10% weight
    if weekly_rsi is not None:
        if weekly_rsi > 60:     score += 0.10
        elif weekly_rsi > 45:   score += 0.05
        # else 0
 
    return round(score, 2)


def roll_event_calendar(service=None, sb=None):
    """Advance recurring event_calendar rows + recompute is_active/event_status
    for every row. Calls the same fn_rollover_event_calendar() Postgres
    function that pg_cron runs daily — calling it again here is a cheap,
    idempotent safety net. Runs FIRST so nothing downstream reads stale data.
    """
    sb = sb or get_supabase()
    try:
        rolled = sb.rpc("fn_rollover_event_calendar", {}).execute().data or []
        for row in rolled:
            logger.info(
                f"  ↻ rolled '{row['rolled_event_name']}': "
                f"{row['prior_start']} → {row['next_start']}"
            )
        logger.info(f"✓ event_calendar rollover: {len(rolled)} event(s) advanced")
        return len(rolled)
    except Exception as e:
        logger.error(f"✗ event_calendar rollover failed: {e}")
        return 0

NSE_HOMEPAGE_URL    = "https://www.nseindia.com"
NSE_HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
 
 
def fetch_nse_holidays(service=None, sb=None, segment="CM"):
    """
    Pull NSE's trading holiday calendar live from nseindia.com — no
    Sheet involved. This is the same JSON endpoint NSE's own site uses
    for its holiday widget: unofficial, but the standard approach used
    across the algo-trading community (nsetools, NseIndiaApi, etc.).
 
    segment: 'CM' (Capital Market / equity cash trading — default, what
    you want for an equity bot) or 'FO' (Futures & Options). NSE
    occasionally has small segment-specific differences.
 
    (service=None) kept in the signature only for consistency with
    main()'s steps loop, which always calls fn(service, sb) — same
    reason as roll_event_calendar's signature. Nothing here touches
    Google Sheets.
    """
    sb = sb or get_supabase()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        # NSE blocks bare API calls without a primed session — hitting
        # the homepage first sets the cookies the API checks for.
        session.get(NSE_HOMEPAGE_URL, timeout=10)
        resp = session.get(NSE_HOLIDAY_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
 
        if segment not in data:
            logger.error(
                f"✗ NSE holidays: segment '{segment}' not in response "
                f"(available: {list(data.keys())}) — NSE may have changed "
                f"their API shape, check manually"
            )
            return 0
 
        rows = []
        for entry in data[segment]:
            date_str = entry.get("tradingDate") or entry.get("date")
            occasion = (
                entry.get("description")
                or entry.get("holidayDescription")
                or "Market Holiday"
            )
            if not date_str:
                continue
            d = dateparser.parse(date_str, dayfirst=True).date()
            rows.append({"date": str(d), "occasion": occasion})
 
        if not rows:
            logger.warning("✗ NSE holidays: fetched 0 rows — check segment/response shape")
            return 0
 
        if not DRY_RUN:
            sb.table("nse_holidays").upsert(rows, on_conflict="date").execute()
        logger.info(f"✓ NSE_HOLIDAYS: {len(rows)} holidays synced live from nseindia.com")
        return len(rows)
 
    except Exception as e:
        # A network hiccup or NSE blocking the request shouldn't break
        # the rest of the daily pipeline — same try/except pattern as
        # every other step in main(). The manually-seeded baseline from
        # 003_nse_holidays_data_seed.sql stays in place either way.
        logger.error(f"✗ NSE holidays live fetch failed: {e}")
        return 0


def ingest_nse_holidays(service, sb):
    logger.info("Ingesting NSE_HOLIDAYS...")
    raw = read_tab(service, "NSE_HOLIDAYS", 50)
    df  = rows_to_df(raw, 1, 2)
    if df.empty:
        return 0

    rows = []
    for _, r in df.iterrows():
        d = parse_date(r.get("date"))
        if not d:
            continue
        rows.append({"date": str(d), "occasion": safe_str(r.get("occasion"))})

    if not DRY_RUN and rows:
        sb.table("nse_holidays").upsert(rows, on_conflict="date").execute()
    logger.info(f"✓ NSE_HOLIDAYS: {len(rows)} holidays")
    return len(rows)

def step_compute_sector_strength(service=None, sb=None):
    """Replaces ingest_sector_strength + ingest_industry_strength.
    Must run AFTER compute_indicators — needs today's stock_data_daily."""
    sb = sb or get_supabase()
    today = today_ist()
    sb.rpc("fn_compute_sector_industry_strength", {"p_date": str(today)}).execute()
    logger.info(f"✓ sector/industry strength computed for {today}")
    return None


# ── Main ─────────────────────────────────────────────────────
def main():
    from config import is_kill_switch_active
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting ingestion")
        return {}

    logger.info("=" * 60)
    logger.info("STEP 1: Google Sheets Ingestion")
    logger.info("=" * 60)

    sb      = get_supabase()
    service = get_sheets_service()

    results = {}
    steps = [
        ("sector_industry_strength",     step_compute_sector_strength),
        ("nse_holidays",        ingest_nse_holidays),
        ("event_calendar",      roll_event_calendar),
    ]

    for name, fn in steps:
        try:
            results[name] = fn(service, sb)
        except Exception as e:
            logger.error(f"✗ {name} failed: {e}")
            results[name] = 0

    total = sum(v for v in results.values() if v is not None)
    logger.info(f"\n✓ Ingestion complete: {total} total rows across {len(results)} tables")
    return results


if __name__ == "__main__":
    main()
