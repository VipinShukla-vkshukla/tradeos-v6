"""
TradeOS v6 — Phase 2: Compute Indicators
Replaces all Google Sheet formula columns with Python computations.
Reads chartink_raw_data + stock_data_daily (bhavcopy), computes in correct
dependency order, upserts computed + renamed columns to stock_data_daily.

Wire in run_pipeline.py as Step 03 (before ingest_sheets), Phase 2+:
    def step_compute_indicators():
        from compute.compute_indicators import main as fn; return fn()

Dependency order for computed columns:
  Level 1 — direct from raw chartink fields:
    vol_ratio      = volume / avg_vol_20
    atr_pct        = atr_14 / daily_close * 100  [note: raw field is daily_close]

  Level 2 — derived from Level 1 + raw:
    dist_sma50     = (close - sma_50) / sma_50 * 100
    dist_sma200    = (close - sma_200) / sma_200 * 100
    above_sma50    = close > sma_50
    above_sma200   = close > sma_200
    above_st       = close > supertrend
    sma50_gt_200   = sma_50 > sma_200
    price_location = (close - week52_low) / (week52_high - week52_low) * 100
    breakout_setup = close > sma_50 AND vol_ratio > 1.5 AND consol_range < 8
    bk_trigger     = close > week52_high * 0.98  (52-week breakout proximity)
    consol_range   = (high_30d - low_30d) / low_30d * 100  (low_30d from rolling history)
    ret_1w         = pct change over last 5 sessions
    ret_1m         = pct change over last 20 sessions
    ret_3m         = pct change over last 60 sessions
    ret_6m         = pct change over last 120 sessions
    ret_12m        = pct change over last 240 sessions

  Level 3 — market-relative (needs nifty_total_market):
    rs_vs_nifty    = ret_1m - nifty_ret_1m

RENAME_MAP (21 col renames from chartink_raw_data → stock_data_daily):
    daily_open     → open
    daily_high     → high
    daily_low      → low
    daily_close    → close
    week52_high    → high_52w
    week52_low     → low_52w
    adx_14         → adx
    adx_plus_di    → di_plus
    adx_minus_di   → di_minus
    avg_vol_20     → avg_vol_20d
    avg_vol_50     → avg_vol_50d
    vwap_daily     → vwap
    macd_histogram → macd_hist
    parabolic_sar  → psar
    upper_bb       → bb_upper
    lower_bb       → bb_lower
    stochastic     → stoch
    net_profit_yr  → net_profit_yearly
    market_cap_cat → market_cap_category
    qtr_net_profit → quarterly_net_profit
    qtr_var_profit → quarterly_variance
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# Full RENAME_MAP: chartink_raw_data field → stock_data_daily canonical field
RENAME_MAP = {
    "daily_open":     "open",
    "daily_high":     "high",
    "daily_low":      "low",
    "daily_close":    "close",
    "week52_high":    "high_52w",
    "week52_low":     "low_52w",
    "adx_14":         "adx",
    "adx_plus_di":    "di_plus",
    "adx_minus_di":   "di_minus",
    "avg_vol_20":     "avg_vol_20d",
    "avg_vol_50":     "avg_vol_50d",
    "vwap_daily":     "vwap",
    "macd_histogram": "macd_hist",
    "parabolic_sar":  "psar",
    "upper_bb":       "bb_upper",
    "lower_bb":       "bb_lower",
    "stochastic":     "stoch",
    "net_profit_yr":  "net_profit_yearly",
    "market_cap_cat": "market_cap_category",
    "qtr_net_profit": "quarterly_net_profit",
    "qtr_var_profit": "quarterly_variance",
    "pct_change":     "pct_change_daily",
}

# Pass-through fields: same name in both tables (no rename needed)
PASSTHROUGH_FIELDS = [
    "high_30d",
    "sma_10", "sma_20", "sma_50", "sma_200",
    "ema_10", "ema_20", "ema_50",
    "rsi_daily", "rsi_weekly", "rsi_monthly",
    "volume",
    "vwap_20d", "vwap_50d",
    "atr_14", "atr_pct",
    "ha_high", "ha_low", "ha_close",
    "supertrend",
    "macd_line", "macd_signal",
    "ttm_net_profit", "eps",
    "market_cap",
    "sector", "industry", "company_name",
]


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
        past    = float(rows[-1]["close"])
        return (current - past) / past * 100 if past else None
    except Exception as e:
        logger.warning(f"Nifty return fetch failed: {e}")
        return None


def fetch_historical_closes(sb, symbol: str, today: str, sessions: int) -> list[float]:
    """
    Fetch last N session closes for a symbol from chartink_raw_data.
    Uses daily_close (the correct chartink field name).
    Returns list from most recent → oldest.
    """
    rows = sb.table("chartink_raw_data").select("date,daily_close") \
        .eq("symbol", symbol).lte("date", today) \
        .order("date", desc=True).limit(sessions + 1).execute().data
    return [float(r["daily_close"]) for r in rows if r.get("daily_close")]


def fetch_low_30d(sb, symbol: str, today: str) -> float | None:
    """
    Compute 30-session rolling low from chartink_raw_data daily_low history.
    Used for consol_range = (high_30d - low_30d) / low_30d * 100.
    """
    try:
        rows = sb.table("chartink_raw_data").select("daily_low") \
            .eq("symbol", symbol).lte("date", today) \
            .order("date", desc=True).limit(30).execute().data
        lows = [float(r["daily_low"]) for r in rows if r.get("daily_low")]
        return min(lows) if lows else None
    except Exception as e:
        logger.debug(f"low_30d fetch failed for {symbol}: {e}")
        return None


def compute_level1(raw: dict) -> dict:
    """
    Level 1: Direct derivations from raw Chartink fields.
    Note: raw fields use chartink names (daily_close, avg_vol_20, atr_14, etc.)
    """
    computed = {}

    volume     = float(raw.get("volume")     or 0)
    avg_vol_20 = float(raw.get("avg_vol_20") or 0)
    atr_14     = float(raw.get("atr_14")     or 0)
    close      = float(raw.get("daily_close") or 0)   # ← correct field name

    computed["vol_ratio"] = round(min(volume / avg_vol_20, 50.0), 4) if avg_vol_20 > 0 else None
    computed["atr_pct"]   = round(atr_14 / close * 100, 4) if close > 0 else None

    return computed


def compute_level2(raw: dict, l1: dict, closes_5: list, closes_20: list,
                   closes_60: list, closes_120: list, closes_240: list,
                   low_30d: float | None) -> dict:
    """
    Level 2: Derived from Level 1 + raw fields.
    All raw reads use correct chartink field names.
    """
    computed = {}

    close       = float(raw.get("daily_close") or 0)  # ← correct field name
    sma_50      = float(raw.get("sma_50")       or 0)
    sma_200     = float(raw.get("sma_200")      or 0)
    week52_high = float(raw.get("week52_high")  or 0)
    week52_low  = float(raw.get("week52_low")   or 0)
    high_30d    = float(raw.get("high_30d")     or 0)
    supertrend  = float(raw.get("supertrend")   or 0)
    vol_ratio   = l1.get("vol_ratio") or 0

    # SMA distances
    computed["dist_sma50"]  = round((close - sma_50)  / sma_50  * 100, 4) if sma_50  > 0 else None
    computed["dist_sma200"] = round((close - sma_200) / sma_200 * 100, 4) if sma_200 > 0 else None

    # Boolean flags
    computed["above_sma50"]  = close > sma_50  if sma_50  > 0 else None
    computed["above_sma200"] = close > sma_200 if sma_200 > 0 else None
    computed["above_st"]     = close > supertrend if supertrend > 0 else None
    computed["sma50_gt_200"] = sma_50 > sma_200  if sma_50 > 0 and sma_200 > 0 else None

    # Price location: % position between 52w low and high
    if week52_high > week52_low > 0:
        computed["price_location"] = round(
            (close - week52_low) / (week52_high - week52_low) * 100, 2
        )
    else:
        computed["price_location"] = None

    # Consolidation range: (high_30d - low_30d) / low_30d * 100
    if high_30d > 0 and low_30d and low_30d > 0:
        computed["consol_range"] = round((high_30d - low_30d) / low_30d * 100, 4)
    else:
        computed["consol_range"] = None

    # Breakout setup: close > sma50 AND vol_ratio > 1.5 AND consol_range < 8
    consol = computed.get("consol_range")
    computed["breakout_setup"] = (
        close > sma_50 and vol_ratio > 1.5 and consol is not None and consol < 8
    ) if sma_50 > 0 else None

    # BK trigger: within 2% of 52-week high (52w breakout proximity)
    computed["bk_trigger"] = close > week52_high * 0.98 if week52_high > 0 else None

    # Returns over multiple periods
    def _ret(closes, label):
        if len(closes) >= 2:
            past = closes[-1]
            return round((close - past) / past * 100, 4) if past > 0 else None
        return None

    computed["ret_1w"]  = _ret(closes_5,   "1w")
    computed["ret_1m"]  = _ret(closes_20,  "1m")
    computed["ret_3m"]  = _ret(closes_60,  "3m")
    computed["ret_6m"]  = _ret(closes_120, "6m")
    computed["ret_12m"] = _ret(closes_240, "12m")

    return computed


def compute_level3(l2: dict, nifty_ret_1m: float | None) -> dict:
    """Level 3: Market-relative metrics. Requires nifty_total_market."""
    computed = {}
    ret_1m = l2.get("ret_1m")
    if ret_1m is not None and nifty_ret_1m is not None:
        computed["rs_vs_nifty"] = round(ret_1m - nifty_ret_1m, 4)
    else:
        computed["rs_vs_nifty"] = None
    return computed


def build_upsert_row(raw: dict, bhavcopy: dict, l1: dict, l2: dict, l3: dict, today: str) -> dict:
    """
    Merge all computed fields + renamed pass-through fields into a single
    upsert row for stock_data_daily. Applies RENAME_MAP to all chartink fields.
    """
    symbol = raw.get("symbol")
    bhav   = bhavcopy.get(symbol, {})

    row = {
        "date":    today,
        "symbol":  symbol,
        # Bhavcopy enriched fields
        "delivery_pct": bhav.get("delivery_pct"),
        "delivery_qty": bhav.get("delivery_qty"),
        "value_cr":     bhav.get("value_cr"),
        "prev_close":   bhav.get("prev_close"),
        # Level 1 computed
        **l1,
        # Level 2 computed
        **l2,
        # Level 3 computed
        **l3,
        "computed_at": datetime.now(IST).isoformat(),
    }

    # Apply RENAME_MAP: renamed fields
    for chartink_col, canonical_col in RENAME_MAP.items():
        val = raw.get(chartink_col)
        if val is not None:
            row[canonical_col] = val

    # Pass-through fields (same name in both tables)
    for field in PASSTHROUGH_FIELDS:
        val = raw.get(field)
        if val is not None:
            row[field] = val

    return row


def main():
    """
    Main entry point called by run_pipeline.py step_compute_indicators().
    Returns dict with counts for pipeline summary.
    """
    if is_kill_switch_active():
        logger.warning("Kill switch active — compute_indicators skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb    = get_supabase()
    today = str(today_ist())

    logger.info(f"compute_indicators: computing for {today} {'[DRY RUN]' if DRY_RUN else ''}")

    # 1. Fetch source data
    raw_rows = fetch_today_raw(sb, today)
    if not raw_rows:
        logger.warning(f"No chartink_raw_data for {today} — compute skipped")
        return {"computed": 0, "skipped": 0}

    bhavcopy_map  = fetch_today_bhavcopy(sb, today)
    nifty_ret_1m  = fetch_nifty_return(sb, today, sessions=20)
    logger.info(f"Nifty 1M return: {nifty_ret_1m}%")

    # 2. Compute and collect upsert rows
    upsert_rows = []
    skipped     = 0

    for raw in raw_rows:
        try:
            symbol = raw.get("symbol")
            if not symbol:
                skipped += 1
                continue

            # Fetch historical closes for return calculations (most recent → oldest)
            closes_240 = fetch_historical_closes(sb, symbol, today, 240)
            closes_120 = closes_240[:121] if len(closes_240) >= 121 else closes_240
            closes_60  = closes_240[:61]  if len(closes_240) >= 61  else closes_240
            closes_20  = closes_240[:21]  if len(closes_240) >= 21  else closes_240
            closes_5   = closes_240[:6]   if len(closes_240) >= 6   else closes_240

            # Fetch 30-session low for consol_range
            low_30d = fetch_low_30d(sb, symbol, today)

            l1 = compute_level1(raw)
            l2 = compute_level2(raw, l1, closes_5, closes_20, closes_60,
                                 closes_120, closes_240, low_30d)
            l3 = compute_level3(l2, nifty_ret_1m)

            row = build_upsert_row(raw, bhavcopy_map, l1, l2, l3, today)
            upsert_rows.append(row)

        except Exception as e:
            logger.warning(f"compute_indicators: {raw.get('symbol', '?')} failed: {e}")
            skipped += 1

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(upsert_rows)} rows, skip {skipped}")
        if upsert_rows:
            sample = upsert_rows[0]
            logger.info(f"  Sample ({sample['symbol']}): vol_ratio={sample.get('vol_ratio')}, "
                        f"ret_6m={sample.get('ret_6m')}, dist_sma50={sample.get('dist_sma50')}")
        return {"computed": len(upsert_rows), "skipped": skipped, "dry_run": True}

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
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Compute Indicators")
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not write to Supabase")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    result = main()
    print(result)
