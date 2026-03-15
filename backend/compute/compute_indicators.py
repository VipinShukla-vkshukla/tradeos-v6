"""
TradeOS v6 — Phase 2: Compute Indicators
Replaces all Google Sheet formula columns with Python computations.
Reads chartink_raw_data + stock_data_daily (bhavcopy), computes in correct
dependency order, upserts computed columns back to stock_data_daily.

Wire in run_pipeline.py as Step 03 (before ingest_sheets), Phase 2+:
  def step_compute_indicators():
      from ingestion.compute_indicators import main as fn; return fn()
  # Insert in all_steps between ingest_bhavcopy and ingest_sheets when phase >= 2

Pre-requisite: Run Google Sheet formula audit first.
Screenshot every formula column and classify as Type A (raw from Chartink)
or Type B (derived formula that must be ported here).

Dependency order for Type B columns:
  Level 1 — direct from raw chartink fields:
    vol_ratio      = volume / avg_vol_20
    atr_pct        = atr_14 / close * 100

  Level 2 — derived from Level 1 + raw:
    dist_sma50     = (close - sma_50) / sma_50 * 100
    dist_sma200    = (close - sma_200) / sma_200 * 100
    above_sma50    = close > sma_50
    above_sma200   = close > sma_200
    ret_1m         = pct change over last 20 sessions
    ret_3m         = pct change over last 60 sessions
    ret_6m         = pct change over last 120 sessions
    consol_range   = (high_30d - low_30d) / low_30d * 100
    breakout_setup = close > week52_high * 0.98

  Level 3 — market-relative (needs nifty_total_market):
    rs_vs_nifty    = ret_1m - nifty_ret_1m

Writes: stock_data_daily (upsert on date+symbol)
Reads:  chartink_raw_data, stock_data_daily (bhavcopy cols), nifty_total_market
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, logger


def fetch_today_raw(sb, today: str) -> list[dict]:
    """Fetch today's chartink_raw_data rows."""
    rows = sb.table("chartink_raw_data").select("*").eq("date", today).execute().data
    logger.info(f"chartink_raw_data: {len(rows)} rows for {today}")
    return rows


def fetch_today_bhavcopy(sb, today: str) -> dict:
    """Fetch today's bhavcopy-enriched stock_data_daily rows, keyed by symbol."""
    rows = sb.table("stock_data_daily").select(
        "symbol,delivery_pct,delivery_qty,value_cr,prev_close"
    ).eq("date", today).execute().data
    return {r["symbol"]: r for r in rows}


def fetch_nifty_return(sb, today: str, sessions: int) -> float | None:
    """Fetch Nifty return over N sessions for rs_vs_nifty calculation."""
    try:
        rows = sb.table("nifty_total_market").select("date,close") \
            .lte("date", today).order("date", desc=True) \
            .limit(sessions + 1).execute().data
        if len(rows) < 2:
            return None
        current = float(rows[0]["close"])
        past = float(rows[-1]["close"])
        return (current - past) / past * 100 if past else None
    except Exception as e:
        logger.warning(f"Nifty return fetch failed: {e}")
        return None


def fetch_historical_closes(sb, symbol: str, today: str, sessions: int) -> list[float]:
    """Fetch last N session closes for a symbol (used for return calculations)."""
    rows = sb.table("chartink_raw_data").select("date,close") \
        .eq("symbol", symbol).lte("date", today) \
        .order("date", desc=True).limit(sessions + 1).execute().data
    return [float(r["close"]) for r in rows if r.get("close")]


def compute_level1(raw: dict) -> dict:
    """
    Level 1: Direct derivations from raw Chartink fields.
    All inputs are guaranteed to exist in chartink_raw_data.
    """
    computed = {}

    # vol_ratio: today's volume vs 20-day avg
    volume = float(raw.get("volume") or 0)
    avg_vol_20 = float(raw.get("avg_vol_20") or 0)
    computed["vol_ratio"] = round(min(volume / avg_vol_20, 50.0), 4) if avg_vol_20 > 0 else None

    # atr_pct: ATR as % of close
    atr_14 = float(raw.get("atr_14") or 0)
    close = float(raw.get("close") or 0)
    computed["atr_pct"] = round(atr_14 / close * 100, 4) if close > 0 else None

    return computed


def compute_level2(raw: dict, closes_20: list, closes_60: list, closes_120: list) -> dict:
    """
    Level 2: Derived from Level 1 + raw fields.
    Some require historical close data for return calculations.
    """
    computed = {}
    close = float(raw.get("close") or 0)
    sma_50 = float(raw.get("sma_50") or 0)
    sma_200 = float(raw.get("sma_200") or 0)
    week52_high = float(raw.get("week52_high") or 0)
    high_30d = float(raw.get("high_30d") or 0)
    low_30d = float(raw.get("low_30d") or 0)  # NOTE: add low_30d to chartink fetch if missing

    # Distance from moving averages (as % of MA)
    computed["dist_sma50"]  = round((close - sma_50) / sma_50 * 100, 4)   if sma_50  > 0 else None
    computed["dist_sma200"] = round((close - sma_200) / sma_200 * 100, 4) if sma_200 > 0 else None

    # Boolean flags
    computed["above_sma50"]  = close > sma_50  if sma_50  > 0 else None
    computed["above_sma200"] = close > sma_200 if sma_200 > 0 else None

    # Breakout setup: within 2% of 52-week high
    computed["breakout_setup"] = close > week52_high * 0.98 if week52_high > 0 else None

    # Consolidation range: 30-day high/low spread
    computed["consol_range"] = round((high_30d - low_30d) / low_30d * 100, 4) if low_30d > 0 else None

    # Returns over multiple periods
    if len(closes_20) >= 2:
        past_1m = closes_20[-1]
        computed["ret_1m"] = round((close - past_1m) / past_1m * 100, 4) if past_1m > 0 else None
    else:
        computed["ret_1m"] = None

    if len(closes_60) >= 2:
        past_3m = closes_60[-1]
        computed["ret_3m"] = round((close - past_3m) / past_3m * 100, 4) if past_3m > 0 else None
    else:
        computed["ret_3m"] = None

    if len(closes_120) >= 2:
        past_6m = closes_120[-1]
        computed["ret_6m"] = round((close - past_6m) / past_6m * 100, 4) if past_6m > 0 else None
    else:
        computed["ret_6m"] = None

    return computed


def compute_level3(computed_l2: dict, nifty_ret_1m: float | None) -> dict:
    """
    Level 3: Market-relative metrics. Requires nifty_total_market.
    """
    computed = {}
    ret_1m = computed_l2.get("ret_1m")
    if ret_1m is not None and nifty_ret_1m is not None:
        computed["rs_vs_nifty"] = round(ret_1m - nifty_ret_1m, 4)
    else:
        computed["rs_vs_nifty"] = None
    return computed


def build_upsert_row(raw: dict, bhavcopy: dict, l1: dict, l2: dict, l3: dict, today: str) -> dict:
    """Merge all computed fields into a single upsert row for stock_data_daily."""
    symbol = raw.get("symbol")
    bhav = bhavcopy.get(symbol, {})

    return {
        "date":          today,
        "symbol":        symbol,
        "company_name":  raw.get("company_name"),
        "sector":        raw.get("sector"),
        "industry":      raw.get("industry"),
        # Raw Chartink fields mirrored for convenience
        "close":         raw.get("close"),
        "volume":        raw.get("volume"),
        "rsi_daily":     raw.get("rsi_daily"),
        "rsi_weekly":    raw.get("rsi_weekly"),
        "rsi_monthly":   raw.get("rsi_monthly"),
        "adx_14":        raw.get("adx_14"),
        "atr_14":        raw.get("atr_14"),
        # Bhavcopy enriched fields (written by ingest_bhavcopy.py)
        "delivery_pct":  bhav.get("delivery_pct"),
        "delivery_qty":  bhav.get("delivery_qty"),
        "value_cr":      bhav.get("value_cr"),
        "prev_close":    bhav.get("prev_close"),
        # Level 1 computed
        **l1,
        # Level 2 computed
        **l2,
        # Level 3 computed
        **l3,
        "computed_at": datetime.now(IST).isoformat(),
    }


def main():
    """
    Main entry point called by run_pipeline.py step_compute_indicators().
    Returns dict with counts for pipeline summary.
    """
    sb = get_supabase()
    today = str(today_ist())

    logger.info(f"compute_indicators: computing for {today}")

    # 1. Fetch source data
    raw_rows = fetch_today_raw(sb, today)
    if not raw_rows:
        logger.warning(f"No chartink_raw_data for {today} — compute skipped")
        return {"computed": 0, "skipped": 0}

    bhavcopy_map = fetch_today_bhavcopy(sb, today)
    nifty_ret_1m = fetch_nifty_return(sb, today, sessions=20)

    logger.info(f"Nifty 1M return: {nifty_ret_1m}%")

    # 2. Compute and collect upsert rows
    upsert_rows = []
    skipped = 0

    for raw in raw_rows:
        try:
            symbol = raw.get("symbol")
            if not symbol:
                skipped += 1
                continue

            # Fetch historical closes needed for return calcs
            closes_120 = fetch_historical_closes(sb, symbol, today, 120)
            closes_60 = closes_120[:61]  if len(closes_120) >= 61 else closes_120
            closes_20 = closes_120[:21]  if len(closes_120) >= 21 else closes_120

            l1 = compute_level1(raw)
            l2 = compute_level2(raw, closes_20, closes_60, closes_120)
            l3 = compute_level3(l2, nifty_ret_1m)

            row = build_upsert_row(raw, bhavcopy_map, l1, l2, l3, today)
            upsert_rows.append(row)

        except Exception as e:
            logger.warning(f"compute_indicators: {raw.get('symbol','?')} failed: {e}")
            skipped += 1

    # 3. Batch upsert to stock_data_daily
    BATCH = 50
    total_upserted = 0
    for i in range(0, len(upsert_rows), BATCH):
        batch = upsert_rows[i:i+BATCH]
        sb.table("stock_data_daily").upsert(batch, on_conflict="date,symbol").execute()
        total_upserted += len(batch)

    logger.success(f"compute_indicators: {total_upserted} rows upserted, {skipped} skipped")
    return {"computed": total_upserted, "skipped": skipped}


if __name__ == "__main__":
    main()
