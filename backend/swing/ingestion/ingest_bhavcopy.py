"""
TradeOS v6 — Ingest Bhavcopy (NSE primary + BSE fallback)

Writes to:
  - raw_prices table          (PRIMARY KEY: date, symbol)
  - stock_data_daily table    (PRIMARY KEY: date, symbol)
        columns written: value_cr, delivery_pct, delivery_qty
        Run this SQL migration once before deploying:
            ALTER TABLE public.stock_data_daily
                ADD COLUMN IF NOT EXISTS delivery_qty bigint NULL;

NSE source:  BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip (ZIP containing CSV)
             Columns: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE,
                      LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY,
                      TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
             TURNOVER_LACS → divide by 100 to get crores
             DELIV_QTY and DELIV_PER are INCLUDED in this file — no MTO needed for NSE

BSE fallback: BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV
             Columns: TckrSymb, OpnPric, HghPric, LwPric, ClsPric, PrvsClsgPric,
                      TtlTradgVol, TtlTrfVal, FinInstrmTp, SctySrs, ...
             TtlTrfVal → in rupees, divide by 1e7 to get crores
             BSE does NOT include delivery data — MTO fetch attempted as supplement

MTO delivery (NSE MTO file, BSE source only, non-fatal):
             Provides DELIV_QTY and DELIV_PER when bhavcopy source is BSE
"""
import sys
import time
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import (
    get_supabase, logger, today_ist, DRY_RUN,
    is_kill_switch_active, IST,
)

# ── URLs ──────────────────────────────────────────────────────────────────
# NSE: plain CSV (not ZIP), domain archives.nseindia.com, date format ddmmyyyy
# Includes DELIV_QTY and DELIV_PER — no MTO needed when this source succeeds
NSE_BHAV_URL = (
    "https://archives.nseindia.com/products/content/"
    "sec_bhavdata_full_{ddmmyyyy}.csv"
)
# BSE: plain CSV, date format yyyymmdd
# Does NOT include delivery data — MTO fetched as supplement
BSE_BHAV_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/"
    "BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"
)
# NSE MTO: delivery data — only needed when bhavcopy source is BSE
NSE_DELIV_URL = (
    "https://nsearchives.nseindia.com/archives/equities/mto/"
    "MTO_{ddmmyyyy}.DAT"
)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control":   "no-cache",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}
BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
}


# ── Helpers ───────────────────────────────────────────────────────────────
def last_trading_day() -> date:
    """
    Return the most recent completed trading day.
    If it's before 6 PM IST, today's bhavcopy isn't published yet — use yesterday.
    Weekend days are skipped. No holiday calendar check — if NSE/BSE return 404
    for a holiday, the fallback chain handles it gracefully.
    """
    now = datetime.now(IST)
    d   = now.date()
    if now.hour < 18:
        d -= timedelta(days=1)
    while d.weekday() >= 5:      # skip Saturday (5) and Sunday (6)
        d -= timedelta(days=1)
    return d

def resolve_trade_date(sb) -> date:
    """
    Walk backwards from last_trading_day() skipping NSE holidays.
    Checks nse_holidays table — same table used by compute_indicators.
    """
    candidate = last_trading_day()
    try:
        rows     = sb.table("nse_holidays").select("date").execute().data
        holidays = {str(r["date"]) for r in rows}
    except Exception as e:
        logger.warning(f"nse_holidays fetch failed — skipping holiday check: {e}")
        return candidate

    while str(candidate) in holidays:
        logger.info(f"  {candidate} is an NSE holiday — stepping back")
        candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:   # skip any weekends landed on
            candidate -= timedelta(days=1)

    return candidate

def nse_session() -> requests.Session:
    """Warm up an NSE session — NSE blocks requests without a prior Referer visit."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1.5)           # NSE rate-limits without this pause
    except Exception as e:
        logger.warning(f"NSE session warmup failed (non-fatal): {e}")
    return s


def _clean_numeric(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """Strip comma thousand-separators and coerce to numeric."""
    for col in cols:
        if col in df.columns:
            df[col] = (
                df[col].astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
                .pipe(pd.to_numeric, errors="coerce")
            )
    return df


# ── NSE download ──────────────────────────────────────────────────────────
def try_nse(trade_date: date, session: requests.Session) -> pd.DataFrame:
    """
    Download NSE sec_bhavdata_full plain CSV for trade_date.

    Columns (space-padded headers, tab or comma delimited):
        SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,
        LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,
        NO_OF_TRADES, DELIV_QTY, DELIV_PER

    TURNOVER_LACS → divide by 100 to get crores.
    DELIV_QTY and DELIV_PER are included — no MTO needed when this source succeeds.
    Filter: SERIES == 'EQ' only.
    """
    ddmmyyyy = trade_date.strftime("%d%m%Y")
    url = NSE_BHAV_URL.format(ddmmyyyy=ddmmyyyy)
    logger.info(f"[NSE] Fetching: {url}")

    r = session.get(url, timeout=30)
    if r.status_code != 200:
        raise ValueError(f"NSE HTTP {r.status_code} for {url}")

    if r.text.strip().startswith("<"):
        raise ValueError("NSE returned HTML — session issue or market holiday")

    df = pd.read_csv(io.StringIO(r.text), sep=",", encoding="utf-8-sig")
    df.columns = df.columns.str.strip()

    required_cols = {
        "SYMBOL", "SERIES", "PREV_CLOSE",
        "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE",
        "TTL_TRD_QNTY", "TURNOVER_LACS",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"NSE schema mismatch — missing: {missing}. Got: {list(df.columns)}")

    df = df[df["SERIES"].str.strip() == "EQ"].copy()

    if len(df) < 500:
        raise ValueError(f"NSE EQ record count suspiciously low: {len(df)}")

    df = df.rename(columns={
        "SYMBOL":        "symbol",
        "PREV_CLOSE":    "prev_close",
        "OPEN_PRICE":    "open",
        "HIGH_PRICE":    "high",
        "LOW_PRICE":     "low",
        "CLOSE_PRICE":   "close",
        "TTL_TRD_QNTY":  "volume",
        "TURNOVER_LACS": "value_cr",     # divide by 100 → crores
        "DELIV_QTY":     "delivery_qty",
        "DELIV_PER":     "delivery_pct",
    })

    numeric_cols = [
        "open", "high", "low", "close", "prev_close",
        "volume", "value_cr", "delivery_qty", "delivery_pct",
    ]
    df = _clean_numeric(df, numeric_cols)

    # TURNOVER_LACS → Crores (1 Crore = 100 Lakh)
    df["value_cr"] = df["value_cr"] / 100

    df["symbol"] = df["symbol"].str.strip().str.upper()
    df["date"]   = trade_date.isoformat()
    df["source"] = "NSE"

    logger.info(
        f"[NSE] ✓ {len(df)} EQ records | "
        f"delivery coverage: {df['delivery_pct'].notna().sum()} stocks"
    )

    logger.info(f"[NSE] value_cr sample: {df['value_cr'].head(5).tolist()}")
    logger.info(f"[NSE] value_cr nulls: {df['value_cr'].isna().sum()} / {len(df)}")
    return df


# ── BSE fallback ──────────────────────────────────────────────────────────
def try_bse(trade_date: date) -> pd.DataFrame:
    """
    Download BSE CM bhavcopy CSV for trade_date.

    Actual BSE columns (verified from live file headers):
        TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb,
        SctySrs, XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, FinInstrmNm,
        OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric,
        SttlmPric, OpnIntRst, ChngInOpnIntRst, TtlTradgVol, TtlTrfVal,
        TtlNbOfTxsExctd, SsnId, NewBrdLotQty, Rmks, Rsvd1-4

    TtlTrfVal is in RUPEES → divide by 1e7 to get crores.
    BSE does NOT include delivery data — caller will supplement via MTO.
    Filter: FinInstrmTp == 'STK' and SctySrs in valid equity series.

    Returns DataFrame with v6 raw_prices column names or raises on failure.
    """
    yyyymmdd = trade_date.strftime("%Y%m%d")
    url = BSE_BHAV_URL.format(yyyymmdd=yyyymmdd)
    logger.info(f"[BSE] Fetching fallback: {url}")

    s = requests.Session()
    s.headers.update(BSE_HEADERS)
    try:
        s.get("https://www.bseindia.com", timeout=15)
        time.sleep(1.0)
    except Exception:
        pass

    r = s.get(url, timeout=30)
    r.raise_for_status()

    if r.text.strip().startswith("<"):
        raise ValueError("BSE returned HTML — market likely closed or URL changed")

    df = pd.read_csv(io.BytesIO(r.content), sep=",", encoding="utf-8-sig", engine="python")
    df.columns = df.columns.str.strip()

    # Validate expected BSE schema
    required_cols = {
        "FinInstrmTp", "SctySrs", "TckrSymb",
        "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
        "TtlTradgVol", "TtlTrfVal",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"BSE schema mismatch — missing columns: {missing}")

    # Filter STK instruments and valid equity series
    df = df[df["FinInstrmTp"] == "STK"].copy()
    df = df[df["SctySrs"].isin(["A", "B", "T", "XT"])].copy()

    if len(df) < 2000:
        raise ValueError(
            f"BSE record count suspiciously low: {len(df)} — possible bad download"
        )

    # Rename to v6 raw_prices schema
    df = df.rename(columns={
        "TckrSymb":     "symbol",
        "OpnPric":      "open",
        "HghPric":      "high",
        "LwPric":       "low",
        "ClsPric":      "close",
        "PrvsClsgPric": "prev_close",
        "TtlTradgVol":  "volume",
        "TtlTrfVal":    "value_cr",     # rupees -> will divide by 1e7 below
    })

    # Clean numerics (BSE has comma thousand-separators: "6,129.1")
    numeric_cols = ["open", "high", "low", "close", "prev_close", "volume", "value_cr"]
    df = _clean_numeric(df, numeric_cols)

    # TtlTrfVal is in rupees → convert to crores (1 Crore = 10,000,000 rupees)
    df["value_cr"] = df["value_cr"] / 1e7

    df["symbol"] = df["symbol"].str.strip().str.upper()
    df["date"]   = trade_date.isoformat()
    df["source"] = "BSE"

    # BSE has no delivery data — caller will attempt MTO supplement
    df["delivery_qty"] = None
    df["delivery_pct"] = None

    df = df.dropna(subset=["close"])

    logger.info(f"[BSE] ✓ {len(df)} equity records (no delivery — will try MTO)")
    return df


# ── BSE delivery supplement ────────────────────────────────────────────────
def try_bse_delivery(trade_date: date, session: requests.Session) -> pd.DataFrame:
    """
    Fetch delivery data to supplement BSE bhavcopy (which has none).

    Strategy:
      1. Try NSE sec_bhavdata_full — same file as NSE primary, extract DELIV_QTY
         and DELIV_PER only. This works whenever NSE primary works.
      2. Fallback: NSE MTO DAT file — older format, may 404 on some dates.

    Returns DataFrame[symbol, delivery_qty, delivery_pct], always non-fatal.
    """
    empty = pd.DataFrame(columns=["symbol", "delivery_qty", "delivery_pct"])

    # ── Attempt 1: sec_bhavdata_full (preferred) ──────────────────────────
    try:
        ddmmyyyy = trade_date.strftime("%d%m%Y")
        url = NSE_BHAV_URL.format(ddmmyyyy=ddmmyyyy)
        logger.info(f"[DELIVERY] Fetching from NSE sec_bhavdata_full: {url}")

        r = session.get(url, timeout=30)
        if r.status_code == 200 and not r.text.strip().startswith("<"):
            df = pd.read_csv(io.StringIO(r.text), sep=",", encoding="utf-8-sig")
            df.columns = df.columns.str.strip()
            df = df[df["SERIES"].str.strip() == "EQ"].copy()
            if {"SYMBOL", "DELIV_QTY", "DELIV_PER"}.issubset(df.columns):
                df = df.rename(columns={
                    "SYMBOL":    "symbol",
                    "DELIV_QTY": "delivery_qty",
                    "DELIV_PER": "delivery_pct",
                })[["symbol", "delivery_qty", "delivery_pct"]].copy()
                df["symbol"] = df["symbol"].str.strip().str.upper()
                df["delivery_qty"] = pd.to_numeric(df["delivery_qty"], errors="coerce")
                df["delivery_pct"] = pd.to_numeric(df["delivery_pct"], errors="coerce")
                df = df.dropna(subset=["delivery_pct"])
                logger.info(f"[DELIVERY] ✓ {len(df)} records from sec_bhavdata_full")
                return df
    except Exception as e:
        logger.warning(f"[DELIVERY] sec_bhavdata_full attempt failed: {e}")

    # ── Attempt 2: NSE MTO DAT fallback ──────────────────────────────────
    try:
        ddmmyyyy = trade_date.strftime("%d%m%Y")
        url = NSE_DELIV_URL.format(ddmmyyyy=ddmmyyyy)
        logger.info(f"[DELIVERY] Trying MTO fallback: {url}")

        r = session.get(url, timeout=30)
        r.raise_for_status()
        if r.text.strip().startswith("<"):
            raise ValueError("MTO returned HTML")

        rows = []
        for line in r.text.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7 or parts[0] != "20" or parts[3].strip() != "EQ":
                continue
            rows.append({
                "symbol":       parts[2].strip().upper(),
                "delivery_qty": pd.to_numeric(parts[4], errors="coerce"),
                "delivery_pct": pd.to_numeric(parts[6], errors="coerce"),
            })
        if rows:
            df = pd.DataFrame(rows).dropna(subset=["delivery_pct"])
            logger.info(f"[DELIVERY] ✓ {len(df)} records from MTO fallback")
            return df
    except Exception as e:
        logger.warning(f"[DELIVERY] MTO fallback also failed: {e}")

    logger.warning("[DELIVERY] Both delivery sources failed — delivery will be NULL")
    return empty

# ── Universe filter ───────────────────────────────────────────────────────
def fetch_chartink_universe(sb, trade_date: date) -> set:
    """
    Returns the set of symbols present in chartink_raw_data for trade_date.
    Falls back to the most recent available date if today's data isn't fetched yet.
    """
    try:
        rows = (sb.table("chartink_raw_data")
                  .select("symbol")
                  .eq("date", trade_date.isoformat())
                  .execute().data)

        if not rows:
            latest = (sb.table("chartink_raw_data")
                        .select("date")
                        .order("date", desc=True)
                        .limit(1)
                        .execute().data)
            if latest:
                rows = (sb.table("chartink_raw_data")
                          .select("symbol")
                          .eq("date", latest[0]["date"])
                          .execute().data)

        syms = {r["symbol"] for r in rows if r.get("symbol")}
        logger.info(f"[UNIVERSE] Chartink symbols: {len(syms)}")
        return syms

    except Exception as e:
        logger.warning(f"[UNIVERSE] chartink_raw_data fetch failed: {e}")
        return set()

# ── Upsert to raw_prices ──────────────────────────────────────────────────
def upsert_to_raw_prices(df: pd.DataFrame) -> int:
    """
    Upsert final DataFrame to Supabase raw_prices table.
    PRIMARY KEY (date, symbol) — safe to re-run on the same date.
    """
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(df)} rows to raw_prices")
        return len(df)

    sb   = get_supabase()
    rows = df.to_dict(orient="records")

    # Replace NaN with None — Supabase rejects Python float NaN
    for row in rows:
        for k, v in list(row.items()):
            if isinstance(v, float) and pd.isna(v):
                row[k] = None

    total      = 0
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        sb.table("raw_prices").upsert(batch, on_conflict="date,symbol").execute()
        total += len(batch)

    return total


# ── NEW: Upsert to stock_data_daily ──────────────────────────────────────
def upsert_to_stock_data_daily(df: pd.DataFrame) -> int:
    """
    Upsert value_cr, delivery_pct, and delivery_qty into stock_data_daily.

    Prerequisite — run this migration once in Supabase SQL editor before deploying:
        ALTER TABLE public.stock_data_daily
            ADD COLUMN IF NOT EXISTS delivery_qty bigint NULL;

    PRIMARY KEY (date, symbol) — safe to re-run on the same date.
    Only writes the three enrichment columns; existing OHLCV columns are untouched
    because on_conflict uses a targeted column list.

    Rows that don't yet exist in stock_data_daily are inserted with only these
    three columns populated (plus date + symbol). Rows that already exist are
    updated in-place via upsert merge.
    """
    # Columns written to stock_data_daily
    sdd_cols = ["date", "symbol", "value_cr", "delivery_pct", "delivery_qty"]
    available = [c for c in sdd_cols if c in df.columns]
    missing   = set(sdd_cols) - set(available)
    if missing:
        logger.warning(
            f"[SDD] Missing columns for stock_data_daily upsert: {missing} — "
            "they will be skipped"
        )

    sdd_df = df[available].copy()

    # Cast delivery_qty to Python int where possible (Supabase bigint rejects floats)
    if "delivery_qty" in sdd_df.columns:
        sdd_df["delivery_qty"] = (
            pd.to_numeric(sdd_df["delivery_qty"], errors="coerce")
            .where(sdd_df["delivery_qty"].notna())   # keep NaT/NaN as None
        )

    if DRY_RUN:
        logger.info(
            f"[DRY RUN] Would upsert {len(sdd_df)} rows to stock_data_daily "
            f"(columns: {available})"
        )
        return len(sdd_df)

    sb   = get_supabase()
    rows = sdd_df.to_dict(orient="records")

    # Replace NaN / float('nan') with None — Supabase rejects Python float NaN
    for row in rows:
        for k, v in list(row.items()):
            if isinstance(v, float) and pd.isna(v):
                row[k] = None
            # Also cast delivery_qty to int if it came through as a float whole number
            if k == "delivery_qty" and isinstance(v, float) and not pd.isna(v):
                row[k] = int(v)

    total      = 0
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        sb.table("stock_data_daily").upsert(
            batch, on_conflict="date,symbol"
        ).execute()
        total += len(batch)

    logger.info(f"[SDD] ✓ {total} rows upserted to stock_data_daily")
    return total


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — ingest_bhavcopy skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    start = time.time()
    logger.info("═" * 60)
    logger.info("  STEP: Ingest Bhavcopy (NSE primary + BSE fallback)")
    logger.info("═" * 60)

    # ── Resolve trade date FIRST (needed by idempotency check) ────────────
    sb         = get_supabase()
    trade_date = resolve_trade_date(sb)
    logger.info(f"Trade date: {trade_date}")

    # ── Idempotency: skip only if BOTH tables are already populated ───────
    if not DRY_RUN:
        try:
            rp_count = (
                sb.table("raw_prices")
                  .select("symbol", count="exact")
                  .eq("date", trade_date.isoformat())
                  .limit(1)
                  .execute()
            ).count or 0

            if rp_count > 100:
                sdd_count = (
                    sb.table("stock_data_daily")
                      .select("symbol", count="exact")
                      .eq("date", trade_date.isoformat())
                      .limit(1)
                      .execute()
                ).count or 0

                if sdd_count > 100:
                    logger.info(
                        f"Already complete for {trade_date} — "
                        f"raw_prices: {rp_count} rows | "
                        f"stock_data_daily: {sdd_count} rows — skipping"
                    )
                    return {
                        "status":   "already_done",
                        "rows":     rp_count,
                        "sdd_rows": sdd_count,
                        "date":     trade_date.isoformat(),
                    }
                else:
                    logger.warning(
                        f"raw_prices has {rp_count} rows for {trade_date} "
                        f"but stock_data_daily has only {sdd_count} rows — "
                        f"re-running to fix stock_data_daily"
                    )
        except Exception as e:
            logger.warning(f"Idempotency check failed (non-fatal): {e}")

    # ── Build NSE session (shared for bhavcopy + MTO requests) ───────────
    session = nse_session()

    # ── Try NSE first ─────────────────────────────────────────────────────
    bhav_df = None
    source  = None

    try:
        bhav_df = try_nse(trade_date, session)
        source  = "NSE"
    except Exception as e:
        logger.warning(f"NSE failed: {e} — trying BSE fallback")

    # ── BSE fallback ──────────────────────────────────────────────────────
    if bhav_df is None:
        try:
            bhav_df = try_bse(trade_date)
            source  = "BSE"
        except Exception as e:
            logger.error(f"BSE also failed: {e}")
            raise RuntimeError(
                f"Both NSE and BSE bhavcopy failed for {trade_date}. "
                f"Check if it was a trading holiday. Last error: {e}"
            )

    # ── Supplement BSE source with delivery data ──────────────────────────
    # NSE sec_bhavdata_full includes DELIV_QTY + DELIV_PER directly — nothing to do.
    # BSE bhavcopy has no delivery data — fetch from NSE sec_bhavdata_full as supplement
    # (same file NSE uses as primary, just reading delivery columns only).
    # MTO DAT file is kept as a secondary fallback if that also fails.
    if source == "BSE":
        deliv_df = try_bse_delivery(trade_date, session)
        if not deliv_df.empty:
            bhav_df = bhav_df.drop(columns=["delivery_qty", "delivery_pct"], errors="ignore")
            bhav_df = bhav_df.merge(deliv_df, on="symbol", how="left")
            covered = bhav_df["delivery_pct"].notna().sum()
            logger.info(f"[DELIVERY] Merged: {covered} stocks have delivery data")
        else:
            logger.warning("[DELIVERY] No delivery data available — delivery_pct/qty will be NULL")

    # ── Select only raw_prices schema columns ─────────────────────────────
    target_cols = [
        "date", "symbol",
        "open", "high", "low", "close", "prev_close",
        "volume", "value_cr",
        "delivery_pct", "delivery_qty",
    ]
    final = bhav_df[[c for c in target_cols if c in bhav_df.columns]].copy()

     # ── Upsert to raw_prices — full universe, no filter ───────────────────
    rows_done = upsert_to_raw_prices(final)

     # ── Filter for stock_data_daily — chartink universe only ─────────────
    sdd_final = final.copy()
    try:
        chartink_syms = fetch_chartink_universe(get_supabase(), trade_date)
        if chartink_syms:
            before    = len(sdd_final)
            sdd_final = sdd_final[sdd_final["symbol"].isin(chartink_syms)].copy()
            logger.info(
                f"[UNIVERSE] stock_data_daily filtered: {before} → {len(sdd_final)} rows "
                f"({len(chartink_syms)} chartink symbols)"
            )
        else:
            logger.warning("[UNIVERSE] Chartink universe empty — skipping stock_data_daily upsert")
            sdd_final = sdd_final.iloc[0:0]  # empty df, skip upsert below
    except Exception as e:
        logger.warning(f"[UNIVERSE] Filter failed — skipping stock_data_daily upsert: {e}")
        sdd_final = sdd_final.iloc[0:0]

    # ── Upsert filtered set to stock_data_daily ───────────────────────────
    sdd_rows = 0
    try:
        sdd_rows = upsert_to_stock_data_daily(sdd_final)
    except Exception as e:
        logger.error(
            f"[SDD] stock_data_daily upsert failed (non-fatal, raw_prices already written): {e}"
        )

    duration = time.time() - start
    delivery_coverage = (
        final["delivery_pct"].notna().sum()
        if "delivery_pct" in final.columns else 0
    )
    logger.success(
        f"✓ Bhavcopy ({source}): {rows_done} rows → raw_prices | "
        f"{sdd_rows} rows → stock_data_daily "
        f"in {duration:.1f}s | date={trade_date} | "
        f"delivery coverage: {delivery_coverage} stocks"
    )
    return {
        "status":            "success",
        "source":            source,
        "rows":              rows_done,
        "sdd_rows":          sdd_rows,
        "date":              trade_date.isoformat(),
        "duration":          round(duration, 1),
        "delivery_coverage": int(delivery_coverage),
    }


if __name__ == "__main__":
    main()