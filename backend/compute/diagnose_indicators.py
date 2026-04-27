"""
TradeOS v6 — Diagnostic: Google Sheet vs compute_indicators
===========================================================
Reads STOCK_DATA tab directly from Google Sheet (no DB involvement),
runs compute_from_raw() on the same date, compares field by field.

Output: diagnose_indicators_<date>.csv

Usage:
    python diagnose_indicators.py                        # today
    python diagnose_indicators.py --date 2026-04-17      # specific date
    python diagnose_indicators.py --symbol RELIANCE      # single stock
    python diagnose_indicators.py --diverged-only        # mismatches only
    python diagnose_indicators.py --diverged-only --symbol RELIANCE
"""

import sys
import os
import csv
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# ── Path setup — works from backend/compute/ ─────────────────────────────
HERE    = Path(__file__).parent
BACKEND = HERE.parent
PROJECT = BACKEND.parent
sys.path.insert(0, str(PROJECT))             # project root (config.py)
sys.path.insert(0, str(BACKEND))             # backend/
sys.path.insert(0, str(HERE))                # backend/compute/
sys.path.insert(0, str(BACKEND / "ingestion")) # backend/ingestion/ (ingest_sheets.py)

from config import get_supabase, today_ist, IST, logger

# ── Import sheet helpers ──────────────────────────────────────────────────
try:
    from ingest_sheets import (
        get_sheets_service, read_tab, rows_to_df,
        parse_num, parse_bool, safe_str, parse_date,
    )
    from ingest_sheets import GOOGLE_SHEET_ID as SHEET_ID
except ImportError as e:
    logger.error(f"Could not import from ingest_sheets: {e}")
    logger.error("Ensure ingest_sheets.py is in backend/ingestion/")
    sys.exit(1)

# ── Import compute logic ──────────────────────────────────────────────────
try:
    from compute_indicators import (
        compute_from_raw,
        fetch_bulk_history,
        fetch_bulk_history_yf,
        fetch_upcoming_events,
        fetch_msl_symbols,
        fetch_index_membership,
        fetch_nifty_return,
        fetch_raw_prices,
        resolve_last_trading_day,
        OTHER_SCRIPT_FIELDS,
        WIDE_TOLERANCE_FIELDS,
    )
except ImportError as e:
    logger.error(f"Could not import from compute_indicators: {e}")
    sys.exit(1)

# ── Tolerances ────────────────────────────────────────────────────────────
DIVERGE_THRESHOLD = 0.02
WIDE_THRESHOLD    = 0.10

SKIP_FIELDS = {
    "date", "symbol", "created_at", "updated_at", "compute_meta",
    *OTHER_SCRIPT_FIELDS,
}

# ── Sheet column → DB field mapping (mirrors ingest_stock_data exactly) ──
SHEET_COL_MAP = {
    "company_name":                          ("company_name",           "str"),
    "index_membership":                      ("index_membership",        "str"),
    "sector":                                ("sector",                  "str"),
    "industry":                              ("industry",                "str"),
    "market_cap":                            ("market_cap",              "num"),
    "market_cap_category":                   ("market_cap_category",     "str"),
    "current_price":                         ("current_price",           "num"),
    "open":                                  ("open",                    "num"),
    "high":                                  ("high",                    "num"),
    "low":                                   ("low",                     "num"),
    "close":                                 ("close",                   "num"),
    "52_weeks_high":                         ("high_52w",                "num"),
    "52_weeks_low":                          ("low_52w",                 "num"),
    "30_days_highest_high":                  ("high_30d",                "num"),
    "30_days_lowest_low":                    ("low_30d",                 "num"),
    "30_days_highest_close":                 ("close_30d",               "num"),
    "price_6m_ago":                          ("price_6m_ago",            "num"),
    "price_12m_ago":                         ("price_12m_ago",           "num"),
    "1_week_return":                         ("ret_1w",                  "num"),
    "1_month_return":                        ("ret_1m",                  "num"),
    "3_months_return":                       ("ret_3m",                  "num"),
    "6_months_return":                       ("ret_6m",                  "num"),
    "12_months_return":                      ("ret_12m",                 "num"),
    "10_sma":                                ("sma_10",                  "num"),
    "20_sma":                                ("sma_20",                  "num"),
    "50_sma":                                ("sma_50",                  "num"),
    "200_sma":                               ("sma_200",                 "num"),
    "10_ema":                                ("ema_10",                  "num"),
    "20_ema":                                ("ema_20",                  "num"),
    "50_ema":                                ("ema_50",                  "num"),
    "rsi_latest":                            ("rsi_daily",               "num"),
    "rsi_weekly":                            ("rsi_weekly",              "num"),
    "rsi_monthly":                           ("rsi_monthly",             "num"),
    "adx_14":                                ("adx",                     "num"),
    "adx_di":                                ("di_plus",                 "num"),
    "adx_di_2":                              ("di_minus",                "num"),
    "volume":                                ("volume",                  "int"),
    "20_avg_volume":                         ("avg_vol_20d",             "int"),
    "50_avg_volume":                         ("avg_vol_50d",             "int"),
    "daily_vwap":                            ("vwap",                    "num"),
    "20_day_vwap":                           ("vwap_20d",                "num"),
    "50_day_vwap":                           ("vwap_50d",                "num"),
    "change_prev_day":                       ("pct_change",              "num"),
    "atr_14":                                ("atr_14",                  "num"),
    "atr":                                   ("atr_pct",                 "num"),
    "heikinashi_high":                       ("ha_high",                 "num"),
    "heikinashi_low":                        ("ha_low",                  "num"),
    "heikinashi_close":                      ("ha_close",                "num"),
    "supertrend":                            ("supertrend",              "num"),
    "macd_line":                             ("macd_line",               "num"),
    "macd_signal":                           ("macd_signal",             "num"),
    "macd_histogram":                        ("macd_hist",               "num"),
    "parabolic_sar":                         ("psar",                    "num"),
    "upper_bollinger":                       ("bb_upper",                "num"),
    "lower_bollinger":                       ("bb_lower",                "num"),
    "stochastic":                            ("stoch",                   "num"),
    "ttm_net_profit":                        ("ttm_net_profit",          "num"),
    "net_profit_yearly":                     ("net_profit_yearly",       "num"),
    "eps":                                   ("eps",                     "num"),
    "quarterly_net_profit":                  ("quarterly_net_profit",    "num"),
    "quarterly_variance_net_profit":         ("quarterly_variance",      "num"),
    "price__sma50_yn":                       ("above_sma50",             "bool"),
    "sma50__sma200_yn":                      ("sma50_gt_200",            "bool"),
    "close__supertrend_yn":                  ("above_st",                "bool"),
    "weekly_higher_high_yn":                 ("wk_hi_high",              "bool"),
    "weekly_higher_low_yn":                  ("wk_hi_low",               "bool"),
    "price_distance_from_sma50_":            ("dist_sma50",              "num"),
    "volume_ratio_today__20_avg_volume":     ("vol_ratio",               "num"),
    "value_traded__cr":                      ("value_cr",                "num"),
    "delivery_":                             ("delivery_pct",            "num"),
    "consolidation_range_":                  ("consol_range",            "num"),
    "breakout_set_up_flag_yn":               ("breakout_setup",          "bool"),
    "breakout_trigger_flag_yn":              ("bk_trigger",              "bool"),
    "upcoming_events":                       ("upcoming_events",         "str"),
    "upcoming_event_type":                   ("upcoming_event_type",     "str"),
    "part_of_master_shortlist":              ("in_master_shortlist",     "bool"),
}


# ── Read STOCK_DATA from Google Sheet ────────────────────────────────────
def read_sheet_stock_data(filter_symbol: str = None) -> dict:
    """Returns {symbol: {db_field: value}} parsed from STOCK_DATA tab."""
    logger.info("Connecting to Google Sheets...")
    service = get_sheets_service()

    logger.info("Reading STOCK_DATA tab (up to 600 rows)...")
    raw = read_tab(service, "STOCK_DATA", 600)
    df  = rows_to_df(raw, 1, 2)

    if df.empty:
        logger.error("STOCK_DATA tab returned empty dataframe")
        return {}

    logger.info(f"  {len(df)} rows read from sheet")

    result = {}
    for _, r in df.iterrows():
        sym = safe_str(r.get("symbol"))
        if not sym:
            continue
        sym = sym.strip().upper()
        if filter_symbol and sym != filter_symbol.upper():
            continue

        row = {}
        for sheet_col, (db_field, dtype) in SHEET_COL_MAP.items():
            raw_val = r.get(sheet_col)
            if dtype == "num":
                row[db_field] = parse_num(raw_val)
            elif dtype == "int":
                v = parse_num(raw_val)
                row[db_field] = int(v) if v else None
            elif dtype == "bool":
                b = parse_bool(raw_val)
                row[db_field] = bool(b) if b is not None else None
            else:
                row[db_field] = safe_str(raw_val)

        result[sym] = row

    logger.info(f"  {len(result)} symbols parsed from sheet")
    return result


# ── Field comparison ──────────────────────────────────────────────────────
def get_status(c_val, s_val, field: str) -> tuple[str, float | None]:
    c_none = c_val is None or c_val == ""
    s_none = s_val is None or s_val == ""

    if c_none and s_none:
        return "BOTH_NULL", None
    if c_none:
        return "SHEET_ONLY", None
    if s_none:
        return "COMPUTED_ONLY", None

    if isinstance(c_val, bool) or isinstance(s_val, bool):
        match = bool(c_val) == bool(s_val)
        return ("MATCH" if match else "DIVERGED"), (0.0 if match else 100.0)

    try:
        c_f, s_f = float(c_val), float(s_val)
        threshold = WIDE_THRESHOLD if field in WIDE_TOLERANCE_FIELDS else DIVERGE_THRESHOLD
        delta = abs(c_f - s_f) / abs(s_f) if s_f != 0 else (0.0 if c_f == 0 else 1.0)
        return ("DIVERGED" if delta > threshold else "MATCH"), round(delta * 100, 4)
    except (TypeError, ValueError):
        pass

    match = str(c_val).strip().lower() == str(s_val).strip().lower()
    return ("MATCH" if match else "DIVERGED"), None


# ── Main ──────────────────────────────────────────────────────────────────
def run_diagnosis(date_str: str, filter_symbol: str = None,
                  diverged_only: bool = False) -> str:

    sb = get_supabase()

    # Resolve to actual trading day with data
    try:
        date_str = resolve_last_trading_day(sb, date_str)
    except RuntimeError as e:
        logger.error(str(e))
        return ""

    # ── Step 1: Sheet ─────────────────────────────────────────────────────
    sheet_map = read_sheet_stock_data(filter_symbol)
    if not sheet_map:
        return ""

    # ── Step 2: Compute inputs from Supabase ─────────────────────────────
    logger.info(f"Fetching chartink_raw_data for {date_str}...")
    raw_rows = (
        sb.table("chartink_raw_data")
          .select("*")
          .eq("date", date_str)
          .execute().data
    )
    if filter_symbol:
        raw_rows = [r for r in raw_rows if r.get("symbol") == filter_symbol.upper()]

    if not raw_rows:
        logger.error(f"No chartink_raw_data found for {date_str}")
        return ""
    logger.info(f"  {len(raw_rows)} symbols in chartink_raw_data")

    logger.info("Fetching bulk history and lookup tables...")
    symbols      = [r["symbol"] for r in raw_rows if r.get("symbol")]
    bulk_history = fetch_bulk_history_yf(sb, date_str, symbols)
    events_map   = fetch_upcoming_events(sb, date_str)
    msl_set      = fetch_msl_symbols(sb, date_str)
    index_map, company_map = fetch_index_membership(sb)
    nifty_ret    = fetch_nifty_return(sb, date_str)
    prices_map   = fetch_raw_prices(sb, date_str)

    # ── Step 3: Compute + compare per symbol ─────────────────────────────
    logger.info("Running compute_from_raw and comparing against sheet...")
    output_rows   = []
    status_counts = defaultdict(int)
    diverged_agg  = defaultdict(list)
    skipped       = 0

    for raw in raw_rows:
        sym = raw.get("symbol")
        if not sym:
            skipped += 1
            continue

        sheet = sheet_map.get(sym)
        if not sheet:
            skipped += 1
            continue

        try:
            computed = compute_from_raw(
                raw, bulk_history.get(sym, []),
                events_map, msl_set, index_map, company_map,
                sheet_row=sheet,
                price_row=prices_map.get(sym, {})
            )
            if nifty_ret is not None and computed.get("ret_1m") is not None:
                computed["rs_vs_nifty"] = round(computed["ret_1m"] - nifty_ret, 4)
            computed["date"]   = date_str
            computed["symbol"] = sym
        except Exception as e:
            logger.warning(f"{sym}: compute failed — {e}")
            skipped += 1
            continue

        all_fields = (set(sheet.keys()) | set(computed.keys())) - SKIP_FIELDS

        for field in sorted(all_fields):
            c_val = computed.get(field)
            s_val = sheet.get(field)

            status, delta_pct = get_status(c_val, s_val, field)
            status_counts[status] += 1

            if diverged_only and status != "DIVERGED":
                continue

            if status == "DIVERGED":
                if delta_pct and delta_pct > 15:
                    note = "formula mismatch — set SHEET_ALWAYS or fix formula"
                elif delta_pct and delta_pct > 5:
                    note = "significant delta — check session basis"
                else:
                    note = "within calibration range"
                if delta_pct is not None:
                    diverged_agg[field].append(delta_pct)
            elif status == "COMPUTED_ONLY":
                note = "sheet NULL — computed will fill gap"
            elif status == "SHEET_ONLY":
                note = "compute returns NULL — check formula"
            else:
                note = ""

            output_rows.append({
                "symbol":      sym,
                "field":       field,
                "status":      status,
                "sheet_value": "" if s_val is None else s_val,
                "computed":    "" if c_val is None else c_val,
                "delta_pct":   f"{delta_pct:.4f}" if delta_pct is not None else "",
                "tolerance":   "WIDE 10%" if field in WIDE_TOLERANCE_FIELDS else "NORMAL 2%",
                "note":        note,
            })

    # ── Write CSV ─────────────────────────────────────────────────────────
    suffix   = f"_{filter_symbol}" if filter_symbol else ""
    suffix  += "_diverged" if diverged_only else ""
    out_path = f"diagnose_indicators_{date_str}{suffix}.csv"

    fieldnames = [
        "symbol", "field", "status",
        "sheet_value", "computed",
        "delta_pct", "tolerance", "note",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    # ── Console summary ───────────────────────────────────────────────────
    total     = sum(status_counts.values())
    processed = len(raw_rows) - skipped

    logger.info("=" * 60)
    logger.info(f"DIAGNOSIS — {date_str}{' | ' + filter_symbol if filter_symbol else ''}")
    logger.info(f"  Symbols   : {processed} compared | {skipped} skipped (not in sheet)")
    logger.info(f"  Decisions : {total}")
    if total:
        for label in ("MATCH", "DIVERGED", "COMPUTED_ONLY", "SHEET_ONLY", "BOTH_NULL"):
            n = status_counts[label]
            pct = f" ({n/total*100:.1f}%)" if n else ""
            logger.info(f"  {label:<16}: {n:>6}{pct}")

    if diverged_agg:
        logger.info(f"\n  Top diverged fields (by avg delta):")
        logger.info(f"  {'Field':<30} {'Avg Δ%':>8}  {'# stocks':>8}  Action")
        logger.info(f"  {'-'*62}")
        for field, deltas in sorted(diverged_agg.items(),
                                    key=lambda x: -sum(x[1]) / len(x[1]))[:20]:
            avg = sum(deltas) / len(deltas)
            action = (
                "→ set SHEET_ALWAYS" if avg > 15
                else "→ fix formula" if avg > 5
                else "→ within calibration"
            )
            logger.info(f"  {field:<30} {avg:>7.1f}%  {len(deltas):>8}  {action}")

    logger.info(f"\n  CSV: {out_path}")
    logger.info("=" * 60)
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TradeOS v6 — Sheet vs compute_indicators diagnostic"
    )
    parser.add_argument("--date",          default=str(today_ist()),
                        help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--symbol",        default=None,
                        help="Single symbol e.g. RELIANCE")
    parser.add_argument("--diverged-only", action="store_true",
                        help="CSV contains only DIVERGED rows")
    args = parser.parse_args()

    out = run_diagnosis(args.date, args.symbol, args.diverged_only)
    if out:
        print(f"\nDone → {out}")