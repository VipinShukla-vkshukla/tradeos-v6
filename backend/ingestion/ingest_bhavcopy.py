"""
TradeOS v5 — Step 1: Ingest Bhavcopy (NSE primary + BSE fallback)

NSE sometimes blocks automated downloads. BSE bhavcopy is always
publicly accessible and contains identical OHLCV data for all
NSE-listed stocks. Delivery data comes from NSE MTO file.

Sources:
  Primary:  NSE archives (sec_bhavdata_full_{ddmmyyyy}.csv)
  Fallback: BSE bhavcopy zip (EQ{ddmmyy}_CSV.ZIP)
  Delivery: NSE MTO file (MTO_{ddmmyyyy}.DAT) — non-fatal if missing
"""
import sys
import time
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_fixed

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA, logger, get_supabase, today_ist, DRY_RUN

# ── URLs ──────────────────────────────────────────────────────────────────
NSE_BHAV_URL  = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
BSE_BHAV_URL = ("https://www.bseindia.com/download/BhavCopy/Equity/""BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV")
NSE_DELIV_URL = "https://nsearchives.nseindia.com/archives/equities/deliverypos/MTO_{ddmmyyyy}.DAT"



NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
}


def last_trading_day() -> date:
    """Last weekday — no NSE contact needed."""
    from config import now_ist
    d = today_ist()
    if now_ist().hour < 18:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1.5)
    except Exception as e:
        logger.warning(f"NSE warmup: {e}")
    return s


# ── NSE download ──────────────────────────────────────────────────────────
def try_nse(trade_date: date, session: requests.Session) -> pd.DataFrame:
    yyyymmdd = trade_date.strftime("%Y%m%d")          # new format needs YYYYMMDD
    url = NSE_BHAV_URL.format(yyyymmdd=yyyymmdd)

    logger.info(f"[NSE] Trying: {url}")

    headers = NSE_HEADERS.copy()
    headers.update({
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })

    r = session.get(url, headers=headers, timeout=30)

    if r.status_code != 200:
        raise ValueError(f"NSE HTTP error {r.status_code}")

    # Response is a ZIP — extract the inner CSV
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_name = next(f for f in z.namelist() if f.endswith(".csv") or f.endswith(".CSV"))
        with z.open(csv_name) as f:
            df = pd.read_csv(f, encoding="utf-8-sig")

    df.columns = df.columns.str.strip()

    # New schema uses camelCase column names
    if "SctySrs" in df.columns:
        df = df[df["SctySrs"].str.strip() == "EQ"].copy()

    df = df.rename(columns={
        "TckrSymb":     "symbol",
        "OpnPric":      "open",
        "HghPric":      "high",
        "LwPric":       "low",
        "ClsPric":      "close",
        "TtlTrdQty":    "volume",
        "TtlTrdVal":    "value_cr",
        "PrvsClsgPric": "prev_close",
    })

    required = {"symbol", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"NSE schema mismatch — got: {list(df.columns)}")

    df["symbol"] = df["symbol"].str.strip().str.upper()
    df["date"] = trade_date.isoformat()
    df["source"] = "NSE"

    for c in ["open", "high", "low", "close", "prev_close", "volume", "value_cr"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    logger.info(f"[NSE] ✓ {len(df)} EQ records")
    return df


# ── BSE fallback ──────────────────────────────────────────────────────────
def try_bse(trade_date: date) -> pd.DataFrame:
    """
    BSE CM Bhavcopy — STK format
    Source:
    BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
    """

    yyyymmdd = trade_date.strftime("%Y%m%d")
    url = BSE_BHAV_URL.format(yyyymmdd=yyyymmdd)

    logger.info(f"[BSE] Trying fallback: {url}")

    s = requests.Session()
    s.headers.update(BSE_HEADERS)

    # Warmup (BSE sometimes expects referer visit)
    try:
        s.get("https://www.bseindia.com", timeout=15)
        time.sleep(1)
    except Exception:
        pass

    r = s.get(url, timeout=30)
    r.raise_for_status()

    if r.text.strip().startswith("<"):
        raise ValueError("BSE returned HTML instead of CSV")

    # 🔥 Proper CSV parsing (fixes your FinInstrmTp error)
    df = pd.read_csv(
        io.BytesIO(r.content),
        sep=",",
        encoding="utf-8-sig",
        engine="python"
    )

    df.columns = df.columns.str.strip()

    # ─────────────────────────────────────────────
    # Validate expected schema
    # ─────────────────────────────────────────────
    required_cols = {"FinInstrmTp", "SctySrs", "TckrSymb",
                     "OpnPric", "HghPric", "LwPric",
                     "ClsPric", "PrvsClsgPric",
                     "TtlTradgVol", "TtlTrfVal"}

    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"BSE schema mismatch. Missing columns: {missing}")

    # ─────────────────────────────────────────────
    # Filter only STK instruments
    # ─────────────────────────────────────────────
    df = df[df["FinInstrmTp"] == "STK"].copy()

    # ─────────────────────────────────────────────
    # Filter valid equity trading series
    # Institutional clean universe
    # ─────────────────────────────────────────────
    VALID_SERIES = ["A", "B", "T", "XT"]
    df = df[df["SctySrs"].isin(VALID_SERIES)].copy()

    # ─────────────────────────────────────────────
    # Rename to TradeOS schema
    # ─────────────────────────────────────────────
    df = df.rename(columns={
        "TckrSymb": "symbol",
        "OpnPric": "open",
        "HghPric": "high",
        "LwPric": "low",
        "ClsPric": "close",
        "PrvsClsgPric": "prev_close",
        "TtlTradgVol": "volume",
        "TtlTrfVal": "value_cr",
    })

    df["symbol"] = df["symbol"].str.strip().str.upper()
    df["date"] = trade_date.isoformat()
    df["source"] = "BSE"

    # ─────────────────────────────────────────────
    # Clean comma thousand separators
    # ─────────────────────────────────────────────
    numeric_cols = [
        "open", "high", "low", "close",
        "prev_close", "volume", "value_cr"
    ]

    for c in numeric_cols:
        if c in df.columns:
            df[c] = (
                df[c]
                .astype(str)
                .str.replace(",", "", regex=False)
            )
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Convert turnover to Crores
    if "value_cr" in df.columns:
        df["value_cr"] = df["value_cr"] / 1e7

    # Drop rows without price
    df = df.dropna(subset=["close"])

    # Sanity check (important for production)
    if len(df) < 2000:
        raise ValueError(f"BSE record count suspiciously low: {len(df)}")

    logger.info(f"[BSE] ✓ {len(df)} equity records")

    return df


# ── Delivery ──────────────────────────────────────────────────────────────
def try_delivery(trade_date: date, session: requests.Session) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["symbol","delivery_pct"])
    try:
        ddmmyyyy = trade_date.strftime("%d%m%Y")
        url = NSE_DELIV_URL.format(ddmmyyyy=ddmmyyyy)
        logger.info(f"Downloading delivery: {url}")
        r = session.get(url, timeout=30)
        r.raise_for_status()
        if r.text.strip()[:1] == "<":
            return empty

        rows = []
        for line in r.text.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7 and parts[0] == "20":
                rows.append({
                    "symbol":       parts[2].strip().upper(),
                    "series":       parts[3].strip(),
                    "delivery_pct": pd.to_numeric(parts[6], errors="coerce"),
                })
        if not rows:
            return empty
        df = pd.DataFrame(rows)
        df = df[df["series"] == "EQ"][["symbol","delivery_pct"]].copy()
        logger.info(f"Delivery: {len(df)} records")
        return df
    except Exception as e:
        logger.warning(f"Delivery non-fatal: {e}")
        return empty


# ── Upsert ────────────────────────────────────────────────────────────────
def upsert(df: pd.DataFrame) -> int:
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(df)} rows to raw_prices")
        return len(df)

    sb  = get_supabase()
    rows = df.to_dict(orient="records")
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and pd.isna(v):
                r[k] = None

    total = 0
    for i in range(0, len(rows), 500):
        sb.table("raw_prices").upsert(rows[i:i+500], on_conflict="date,symbol").execute()
        total += len(rows[i:i+500])
    return total


def log_run(status, rows=0, duration=0, error=""):
    if DRY_RUN:
        return
    try:
        get_supabase().table("pipeline_runs").insert({
            "run_date": today_ist().isoformat(),
            "step": "ingest_bhavcopy",
            "status": status,
            "rows_processed": rows,
            "duration_sec": round(duration, 2),
            "error_msg": error or None,
        }).execute()
    except Exception as e:
        logger.warning(f"log_run: {e}")


def main():
    start = time.time()
    logger.info("═" * 60)
    logger.info("STEP 1: Ingest Bhavcopy (NSE + BSE fallback)")
    logger.info("═" * 60)

    trade_date = last_trading_day()
    logger.info(f"Trade date: {trade_date}")

    session = nse_session()

    # ── Try NSE first ──────────────────────────────────────────────
    bhav_df = None
    try:
        bhav_df = try_nse(trade_date, session)
        logger.info("Source: NSE ✓")
    except Exception as e:
        logger.warning(f"NSE failed: {e} — trying BSE fallback")

    # ── BSE fallback ───────────────────────────────────────────────
    if bhav_df is None:
        try:
            bhav_df = try_bse(trade_date)
            logger.info("Source: BSE ✓")
        except Exception as e:
            logger.error(f"BSE also failed: {e}")
            log_run("failed", error=str(e))
            raise RuntimeError(f"Both NSE and BSE bhavcopy failed: {e}")

    # ── Delivery (non-fatal) ───────────────────────────────────────
    deliv_df = try_delivery(trade_date, session)
    if not deliv_df.empty:
        bhav_df = bhav_df.merge(deliv_df, on="symbol", how="left")
    else:
        bhav_df["delivery_pct"] = None

    # ── Save parquet ───────────────────────────────────────────────
    DATA.mkdir(exist_ok=True)
    out = DATA / f"bhavcopy_{trade_date.isoformat()}.parquet"
    bhav_df.to_parquet(out, index=False)
    logger.info(f"Saved: {out} ({len(bhav_df)} rows)")

    # ── Upsert ─────────────────────────────────────────────────────
    cols = ["date","symbol","open","high","low","close",
            "prev_close","volume","value_cr","delivery_pct"]
    final = bhav_df[[c for c in cols if c in bhav_df.columns]].copy()
    rows_done = upsert(final)

    duration = time.time() - start
    logger.success(f"✓ Bhavcopy: {rows_done} rows in {duration:.1f}s")
    log_run("success", rows=rows_done, duration=duration)
    return {"rows": rows_done, "date": trade_date.isoformat()}


if __name__ == "__main__":
    main()