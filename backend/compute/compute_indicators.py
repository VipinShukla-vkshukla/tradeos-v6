"""
TradeOS v6 — Phase 2: Compute Indicators v2.2
==============================================
Hybrid reconciliation with field-level trust graduation.

CHANGE LOG v2.1 → v2.2
  - fetch_bulk_history_yf() inlined directly — no external module dependency.
    Eliminates "No module named 'fetch_bulk_history_yf'" GitHub Actions error.
  - Removed importlib dynamic loader block entirely.
  - All functionality identical to v2.1 + separate fetch_bulk_history_yf.py.

DESIGN OVERVIEW
  Runs AFTER ingest_sheets.py. Sheet values are already in stock_data_daily.
  Script computes from chartink_raw_data + Supabase lookup tables, then
  reconciles field-by-field using per-field trust levels from system_config.

FIELD TRUST LEVELS (stored in system_config, key = compute_trust_<field>)
  RECONCILE       — compare vs sheet, use computed if within tolerance,
                    use sheet if diverged, log divergence for investigation
  COMPUTE_ALWAYS  — ignore sheet, always use computed (field is verified)
  SHEET_ALWAYS    — always use sheet, do not overwrite (field is different basis)

  Default for all computable fields: RECONCILE
  Once you verify a field consistently matches, set to COMPUTE_ALWAYS.
  If a field uses a genuinely different formula in the Sheet, set SHEET_ALWAYS.

DIVERGED BEHAVIOUR
  When a field diverges beyond tolerance AND trust = RECONCILE:
    - Sheet value is used (safe fallback)
    - Field logged in divergence summary with delta%
    - compute_meta JSONB records both values for your review
  This is a calibration signal, not a permanent rule. As you fix compute
  formulas, divergence drops to 0 and COMPUTE_ALWAYS takes over.

FIELDS PREVIOUSLY SHEET_ONLY — NOW COMPUTED FROM SUPABASE
  upcoming_events      — nifty_upcoming_events.details (next event in 14 days)
  upcoming_event_type  — nifty_upcoming_events.purpose
  in_master_shortlist  — master_shortlist table (symbol present for today)
  index_membership     — nifty_total_market.nifty_200 / nifty_500 booleans

FIELDS GENUINELY NOT COMPUTABLE
  fii_sector_flow      — sector-level FII requires paid data (Bloomberg etc.)
                         Set to NULL explicitly. Do not source from Sheet
                         (Sheet value is an estimate anyway).

HISTORICAL RETURNS — GRACEFUL DEGRADATION
  fetch_bulk_history_yf() uses price_history_yf (cached) + yfinance.
  On Day 1 of Phase 2 you may have < 120 sessions → ret_6m returns None.
  Reconciliation will use the Sheet value for those fields automatically
  until price_history_yf accumulates enough history (~6 months).
  No code change needed — degradation is handled transparently.

REMOVING RECONCILIATION
  Per-field: UPDATE system_config SET value='COMPUTE_ALWAYS'
             WHERE key='compute_trust_vol_ratio';
  All fields: UPDATE system_config SET value='false'
              WHERE key='compute_indicators_reconcile';
  CLI: python compute_indicators.py --no-reconcile

PERFORMANCE
  6 bulk queries total for all 500 symbols (not 1000 per-symbol like v1):
    1. chartink_raw_data today
    2. stock_data_daily today (sheet baseline)
    3. price_history_yf cache (chunked, 20 symbols at a time)
    4. nifty_upcoming_events (next 14 days)
    5. master_shortlist today (in_master_shortlist flag)
    6. nifty_total_market (index_membership — static, cached)
    + 1 scalar: market_regime for nifty return
"""


import sys
import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# ── Path setup ────────────────────────────────────────────────────────────────
current_dir = Path(__file__).resolve().parent
backend_dir = current_dir.parent
repo_root   = backend_dir.parent

for p in [str(repo_root), str(backend_dir), str(current_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Config import ─────────────────────────────────────────────────────────────
from config import get_supabase, today_ist, IST, is_kill_switch_active, cfg_bool, cfg, logger

# ── yfinance (optional — graceful degradation if not installed) ───────────────
try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# ── Cache config ──────────────────────────────────────────────────────────────
MIN_SESSIONS_REQUIRED = 260   # minimum cached sessions to skip full yfinance fetch
                              # ret_12m needs 240; symbols below this threshold
                              # will trigger a full yfinance pull on first run

# ── Tolerance thresholds ──────────────────────────────────────────────────────
DIVERGE_THRESHOLD = 0.02   # 2%  for most numeric fields
WIDE_THRESHOLD    = 0.10   # 10% for return/range fields (different session basis)
WIDE_TOLERANCE_FIELDS = {
    "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_12m",
    "consol_range", "dist_sma50", "rs_vs_nifty", "low_30d",
    "price_6m_ago", "price_12m_ago", "close_30d",
}

# ── RENAME_MAP: chartink_raw_data → stock_data_daily canonical names ──────────
RENAME_MAP = {
    "daily_open":     "open",
    "daily_high":     "high",
    "daily_low":      "low",
    "daily_close":    "close",
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
    "pct_change":     "pct_change",   # v1 BUG FIX: was 'pct_change_daily'
}

PASSTHROUGH_FIELDS = [
    # Same name in chartink_raw_data and stock_data_daily — copied as-is
    "high_30d", "sma_10", "sma_20", "sma_50", "sma_200",
    "ema_10", "ema_20", "ema_50", "rsi_daily", "rsi_weekly", "rsi_monthly",
    "volume", "vwap_20d", "vwap_50d", "atr_14",
    "ha_high", "ha_low", "ha_close", "supertrend",
    "macd_line", "macd_signal", "ttm_net_profit", "eps",
    "market_cap", "sector", "industry", "company_name",
]

# Fields owned by other scripts — compute_indicators never reads or writes these
OTHER_SCRIPT_FIELDS = {
    "asm_flag",         # ingest_asm_gsm.py
    "fo_ban_flag",      # ingest_asm_gsm.py
    "kite_price",       # kite_reconcile.py
    "predicted_regime", # ml_regime_predict.py
    "delivery_pct",     # ingest_bhavcopy.py
    "delivery_qty",     # ingest_bhavcopy.py
    "value_cr",         # ingest_bhavcopy.py
}

# Fields that cannot be computed from any available free data source
# Set to NULL explicitly — do not source from Sheet (Sheet value is estimated)
NOT_COMPUTABLE_FIELDS = {
    "fii_sector_flow",  # requires sector-level FII data (paid: Bloomberg, SEBI direct)
}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: BULK HISTORY — price_history_yf cache + yfinance fallback
# (previously a separate module: fetch_bulk_history_yf.py)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_bulk_history_yf(sb, today: str, symbols: list,
                           days_back: int = 370,
                           cache_to_db: bool = True,
                           dry_run: bool = False) -> dict:
    """
    Fetch OHLC history for all symbols.
    Priority: price_history_yf (cached) → yfinance (live fetch + write-through cache).

    Args:
        sb          : Supabase client
        today       : date string YYYY-MM-DD
        symbols     : list of NSE symbol strings e.g. ['RELIANCE', 'TCS']
        days_back   : calendar days of history to request
        cache_to_db : if True, saves yfinance results to price_history_yf table
        dry_run     : if True, skips writing to price_history_yf

    Returns:
        {symbol: [{close, low, high, date}, ...]} newest first
    """
    cutoff  = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days_back + 100)).date())
    result  = {}

    # ── Step 1: lightweight sufficiency check (symbol + date only) ────────
    logger.info(
        f"  fetch_bulk_history_yf: checking price_history_yf cache "
        f"({len(symbols)} symbols)..."
    )

    sym_counts: dict = {}   # {symbol: row_count}
    sym_latest: dict = {}   # {symbol: most_recent_date_str}

    for i in range(0, len(symbols), 20):
        chunk = symbols[i:i + 20]
        rows = (sb.table("price_history_yf")
                  .select("symbol,date")
                  .in_("symbol", chunk)
                  .gte("date", cutoff)
                  .lte("date", today)
                  .order("date", desc=True)
                  .limit(10000)
                  .execute().data)
        counts: dict = defaultdict(int)
        latest: dict = {}
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            counts[sym] += 1
            if sym not in latest:
                latest[sym] = r["date"]   # first = most recent (ordered desc)
        sym_counts.update(counts)
        sym_latest.update(latest)

    # Classify symbols
    sufficient_syms: list = []
    need_yf_full:    list = []   # no base history  → full days_back pull
    need_yf_tail:    list = []   # has base history → tail 7-day refresh only

    for sym in symbols:
        count = sym_counts.get(sym, 0)
        if count < MIN_SESSIONS_REQUIRED:
            need_yf_full.append(sym)
        else:
            sufficient_syms.append(sym)
            need_yf_tail.append(sym)   # always refresh tail regardless of staleness

    logger.info(
        f"  price_history_yf: {len(sufficient_syms)} have base | "
        f"{len(need_yf_tail)} tail-refresh | {len(need_yf_full)} full fetch"
    )

    # ── Step 2: fetch full OHLCV for cache-sufficient symbols ─────────────
    # 20 symbols × ~319 sessions ≈ 6 380 rows/query — within Supabase cap
    for i in range(0, len(sufficient_syms), 20):
        chunk = sufficient_syms[i:i + 20]
        rows = (sb.table("price_history_yf")
                  .select("symbol,date,open,high,low,close,volume")
                  .in_("symbol", chunk)
                  .gte("date", cutoff)
                  .lte("date", today)
                  .order("date", desc=True)
                  .limit(10000)
                  .execute().data)
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            result.setdefault(sym, []).append({
                "close": float(r["close"]) if r.get("close") else None,
                "low":   float(r["low"])   if r.get("low")   else None,
                "high":  float(r["high"])  if r.get("high")  else None,
                "date":  r.get("date"),
            })

    # ── Step 3: yfinance for missing / stale symbols ──────────────────────
    need_yf = need_yf_full + need_yf_tail

    if need_yf and YF_AVAILABLE:
        logger.info(
            f"  yfinance: {len(need_yf_full)} full + {len(need_yf_tail)} tail fetches..."
        )
        end_date    = str((datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).date())
        tail_cutoff = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).date())

        def _download_batch(syms: list, start: str):
            if not syms:
                return None, []
            tkrs = [f"{s}.NS" for s in syms]
            df = yf.download(
                tkrs,
                start=start,
                end=end_date,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            return df, tkrs

        rows_to_cache: list = []

        batches = [
            (need_yf_full, *_download_batch(need_yf_full, cutoff)),
            (need_yf_tail, *_download_batch(need_yf_tail, tail_cutoff)),
        ]

        for batch_syms, raw_df, batch_tickers in batches:
            if raw_df is None or not batch_syms:
                continue

            for sym, ticker in zip(batch_syms, batch_tickers):
                try:
                    if len(batch_tickers) == 1:
                        sym_df = raw_df.copy()
                    else:
                        sym_df = (
                            raw_df.xs(ticker, axis=1, level=1)
                            if ticker in raw_df.columns.get_level_values(1)
                            else None
                        )

                    if sym_df is None or sym_df.empty:
                        logger.debug(f"  yfinance: no data for {sym}")
                        continue

                    sym_df = sym_df.dropna(subset=["Close"]).sort_index(ascending=False)

                    history: list = []
                    for date_idx, row in sym_df.iterrows():
                        date_str = str(date_idx.date())
                        entry = {
                            "close": round(float(row["Close"]), 4),
                            "low":   round(float(row["Low"]),   4),
                            "high":  round(float(row["High"]),  4),
                            "date":  date_str,
                        }
                        history.append(entry)

                        if cache_to_db and not dry_run:
                            rows_to_cache.append({
                                "symbol": sym,
                                "date":   date_str,
                                "open":   round(float(row["Open"]), 4),
                                "high":   entry["high"],
                                "low":    entry["low"],
                                "close":  entry["close"],
                                "volume": int(row["Volume"]) if row.get("Volume") else None,
                            })

                    # Tail-refresh: merge new rows into existing result
                    if sym in result:
                        existing_dates = {r["date"] for r in result[sym]}
                        for h in history:
                            if h["date"] not in existing_dates:
                                result[sym].insert(0, h)
                        result[sym].sort(key=lambda x: x["date"], reverse=True)
                    else:
                        result[sym] = history

                    logger.debug(f"  yfinance: {sym} — {len(history)} sessions")

                except Exception as exc:
                    logger.warning(f"  yfinance: {sym} failed — {exc}")

        # ── Step 4: write-through cache ───────────────────────────────────
        if cache_to_db and not dry_run and rows_to_cache:
            logger.info(f"  Caching {len(rows_to_cache)} rows to price_history_yf...")
            for i in range(0, len(rows_to_cache), 500):
                sb.table("price_history_yf").upsert(
                    rows_to_cache[i:i + 500],
                    on_conflict="symbol,date"
                ).execute()
            logger.info("  Cached successfully")
        elif dry_run and rows_to_cache:
            logger.info(f"  [DRY RUN] Skipping cache write of {len(rows_to_cache)} rows")

    elif need_yf and not YF_AVAILABLE:
        logger.warning(
            f"  yfinance not installed — {len(need_yf)} symbols will have no history. "
            f"Run: pip install yfinance"
        )

    total_sessions = sum(len(v) for v in result.values())
    yf_fetched     = sum(1 for s in need_yf if result.get(s))
    logger.info(
        f"  fetch_bulk_history_yf: {len(result)} symbols | "
        f"{total_sessions} total sessions | "
        f"{yf_fetched} fetched from yfinance"
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: SUPABASE BULK FETCHES
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_all_raw(sb, today: str) -> list:
    rows = sb.table("chartink_raw_data").select("*").eq("date", today).execute().data
    logger.info(f"  chartink_raw_data: {len(rows)} rows")
    return rows


def fetch_sheet_values(sb, today: str) -> dict:
    """Read today's stock_data_daily (written by ingest_sheets). {symbol: row}."""
    rows = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    logger.info(f"  stock_data_daily (sheet baseline): {len(rows)} rows")
    return {r["symbol"]: r for r in rows}


def fetch_upcoming_events(sb, today: str, window_days: int = 30) -> dict:
    """
    {symbol: {events_str, event_type_str}} for next window_days.
    Takes the nearest event per symbol.
    """
    cutoff = str(
        (datetime.strptime(today, "%Y-%m-%d").date()) + timedelta(days=window_days)
    )
    rows = (sb.table("nifty_upcoming_events")
              .select("symbol,purpose,details,event_date")
              .gte("event_date", today)
              .lte("event_date", cutoff)
              .order("event_date", desc=False)
              .execute().data)
    result: dict = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym or sym in result:
            continue
        purpose = r.get("purpose") or ""
        details = r.get("details") or purpose
        result[sym] = {
            "upcoming_events":     details[:200] if details else None,
            "upcoming_event_type": purpose[:100] if purpose else None,
        }
    logger.info(
        f"  Upcoming events: {len(result)} symbols with events in next {window_days}d"
    )
    return result


def fetch_msl_symbols(sb, today: str) -> set:
    """Set of symbols present in master_shortlist for today."""
    rows = (sb.table("master_shortlist")
              .select("symbol")
              .eq("date", today)
              .execute().data)
    if not rows:
        latest = (sb.table("master_shortlist")
                    .select("date")
                    .order("date", desc=True)
                    .limit(1)
                    .execute().data)
        if latest:
            rows = (sb.table("master_shortlist")
                      .select("symbol")
                      .eq("date", latest[0]["date"])
                      .execute().data)
    syms = {r["symbol"] for r in rows if r.get("symbol")}
    logger.info(f"  MSL symbols: {len(syms)} in shortlist")
    return syms


def fetch_index_membership(sb) -> tuple:
    """Returns (index_map, company_map)."""
    rows = sb.table("nifty_total_market").select(
        "symbol,nifty_200,nifty_500,company_name"
    ).execute().data
    index_map:   dict = {}
    company_map: dict = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        in_200 = bool(r.get("nifty_200"))
        in_500 = bool(r.get("nifty_500"))
        if in_200:
            index_map[sym] = "Nifty200,Nifty500"
        elif in_500:
            index_map[sym] = "Nifty500"
        else:
            index_map[sym] = ""
        company_map[sym] = r.get("company_name") or ""
    logger.info(f"  Index membership: {len(index_map)} symbols mapped")
    return index_map, company_map


def fetch_nifty_return(sb, today: str, sessions: int = 20) -> float | None:
    """Nifty 1M return for rs_vs_nifty. 1 scalar query."""
    try:
        rows = (sb.table("market_regime")
                  .select("nifty_price")
                  .lte("date", today)
                  .order("date", desc=True)
                  .limit(sessions + 1)
                  .execute().data)
        if len(rows) >= 2:
            c = float(rows[0]["nifty_price"])
            p = float(rows[-1]["nifty_price"])
            return round((c - p) / p * 100, 4) if p else None
    except Exception as exc:
        logger.debug(f"Nifty return failed: {exc}")
    return None


def fetch_raw_prices(sb, today: str) -> dict:
    """{symbol: {value_cr, delivery_pct, delivery_qty}} from raw_prices for today."""
    rows = (sb.table("raw_prices")
              .select("symbol,value_cr,delivery_pct,delivery_qty")
              .eq("date", today)
              .execute().data)
    result: dict = {}
    for r in rows:
        sym = r.get("symbol")
        if sym:
            result[sym] = {
                "value_cr":     float(r.get("value_cr")     or 0),
                "delivery_pct": float(r.get("delivery_pct") or 0),
                "delivery_qty": float(r.get("delivery_qty") or 0),
            }
    logger.info(f"  raw_prices: {len(result)} symbols with delivery/value data")
    return result


def fetch_field_trust(sb) -> dict:
    """
    {field_name: 'RECONCILE'|'COMPUTE_ALWAYS'|'SHEET_ALWAYS'} from system_config.
    Keys: compute_trust_<field_name>. Default: RECONCILE for all fields.
    """
    try:
        rows = (sb.table("system_config")
                  .select("key,value")
                  .like("key", "compute_trust_%")
                  .execute().data)
        trust: dict = {}
        for r in rows:
            field = r["key"].replace("compute_trust_", "")
            trust[field] = (r.get("value") or "RECONCILE").upper()
        return trust
    except Exception as exc:
        logger.debug(f"Field trust fetch failed (using defaults): {exc}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: PER-SYMBOL COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

def compute_from_raw(raw: dict, sym_history: list, events: dict,
                     msl_set: set, index_map: dict, company_map: dict = None,
                     sheet_row: dict = None,
                     price_row: dict = None) -> dict:
    """
    Compute all derivable fields for one symbol.
    sym_history: [{close, low, high, date?}] newest first.
    """
    out: dict = {}
    sym = raw.get("symbol", "")

    # ── Raw values (chartink field names) ─────────────────────────────────
    close      = float(raw.get("daily_close") or 0)
    volume     = float(raw.get("volume")       or 0)
    avg_vol_20 = float(raw.get("avg_vol_20")   or 0)
    atr_14     = float(raw.get("atr_14")       or 0)
    sma_50     = float(raw.get("sma_50")       or 0)
    _sma200_raw = raw.get("sma_200")
    sma_200    = float(_sma200_raw) if _sma200_raw not in (None, "", 0) else 0.0
    if _sma200_raw not in (None, "", 0):
        out["sma_200"] = round(float(_sma200_raw), 4)
    supertrend = float(raw.get("supertrend")   or 0)

    # ── 52-week high/low from rolling history (252 sessions) ──────────────
    window_52w = sym_history[:252]
    highs_52w  = [h["high"] for h in window_52w if h.get("high")]
    lows_52w   = [h["low"]  for h in window_52w if h.get("low")]

    if highs_52w:
        out["high_52w"] = round(max(highs_52w), 4)
    elif raw.get("week52_high"):
        out["high_52w"] = round(float(raw["week52_high"]), 4)

    if lows_52w:
        out["low_52w"] = round(min(lows_52w), 4)
    elif raw.get("week52_low"):
        out["low_52w"] = round(float(raw["week52_low"]), 4)

    week52_hi = out.get("high_52w") or 0
    week52_lo = out.get("low_52w")  or 0

    # ── Level 1 ───────────────────────────────────────────────────────────
    out["vol_ratio"]     = round(volume / avg_vol_20, 4) if avg_vol_20 > 0 else None
    out["atr_pct"]       = round(atr_14 / close * 100, 4) if close > 0 else None
    out["current_price"] = close if close > 0 else None

    # ── Level 2 — SMA / price flags ───────────────────────────────────────
    out["dist_sma50"]   = round((close - sma_50)  / sma_50  * 100, 4) if sma_50  > 0 else None
    out["above_sma50"]  = bool(close > sma_50)  if sma_50  > 0 else None
    out["sma50_gt_200"] = bool(sma_50 > sma_200) if sma_50 > 0 and sma_200 > 0 else None
    if "sma_200" not in out and sma_200 > 0:
        out["sma_200"] = round(sma_200, 4)
    out["above_st"]     = bool(close > supertrend) if supertrend > 0 else None
    if week52_hi > week52_lo > 0:
        out["price_location"] = round(
            (close - week52_lo) / (week52_hi - week52_lo) * 100, 2
        )

    # ── Level 2 — historical series ───────────────────────────────────────
    closes = [h["close"] for h in sym_history]
    lows   = [h["low"]   for h in sym_history if h.get("low")]
    highs  = [h["high"]  for h in sym_history if h.get("high")]

    def _ret(n: int) -> float | None:
        if len(closes) > n and closes[n] not in (None, 0):
            return round((close - closes[n]) / closes[n] * 100, 4)
        return None

    out["ret_1w"]        = _ret(5)
    out["ret_1m"]        = _ret(20)
    out["ret_3m"]        = _ret(60)
    out["ret_6m"]        = _ret(120)
    out["ret_12m"]       = _ret(240)
    out["price_6m_ago"]  = round(closes[120], 2) if len(closes) > 120 else None
    out["price_12m_ago"] = round(closes[240], 2) if len(closes) > 240 else None
    out["close_30d"]     = round(closes[30],  2) if len(closes) > 30  else None

    # ── 30-day window high/low/consol ─────────────────────────────────────
    window   = sym_history[:30] if len(sym_history) >= 30 else sym_history
    lows_30  = [h["low"]  for h in window if h.get("low")  is not None]
    highs_30 = [h["high"] for h in window if h.get("high") is not None]

    if lows_30:
        out["low_30d"]  = round(min(lows_30), 2)
    if highs_30:
        out["high_30d"] = round(max(highs_30), 2)

    low_30d  = out.get("low_30d")
    high_30d = out.get("high_30d")
    if high_30d and low_30d and low_30d > 0:
        out["consol_range"] = round((high_30d - low_30d) / low_30d * 100, 4)

    # ── wk_hi_high / wk_hi_low ───────────────────────────────────────────
    window_10 = sym_history[:15]
    highs_10  = [h["high"] for h in window_10 if h.get("high") is not None][:10]
    lows_10   = [h["low"]  for h in window_10 if h.get("low")  is not None][:10]

    out["wk_hi_high"] = (
        bool(max(highs_10[:5]) > max(highs_10[5:10])) if len(highs_10) >= 10 else None
    )
    out["wk_hi_low"]  = (
        bool(min(lows_10[:5]) > min(lows_10[5:10]))   if len(lows_10)  >= 10 else None
    )

    # ── bk_trigger ────────────────────────────────────────────────────────
    _pr         = price_row or {}
    _high_30d   = out.get("high_30d")
    _close_30d  = out.get("close_30d")
    _vol_ratio  = out.get("vol_ratio")
    _above_st   = out.get("above_st")
    _open       = float(raw.get("daily_open")  or 0)
    _rsi_daily  = float(raw.get("rsi_daily")   or 0)
    _rsi_weekly = float(raw.get("rsi_weekly")  or 0)
    _dist_sma50 = out.get("dist_sma50")
    _atr_pct    = out.get("atr_pct")
    _delivery   = _pr.get("delivery_pct", 0)
    _value_cr   = _pr.get("value_cr",     0)

    if all(v is not None for v in [
        _high_30d, _close_30d, _vol_ratio, _above_st, _dist_sma50, _atr_pct
    ]):
        _sma50_threshold = max(8.0, 1.5 * _atr_pct)
        _delivery_ok     = (_delivery >= 35 or _value_cr >= 50)
        out["bk_trigger"] = bool(
            close        >  _high_30d              and
            close        >  _close_30d             and
            _vol_ratio   >= 1.5                    and
            _above_st                              and
            (_open == 0 or close > _open)          and
            55 <= _rsi_daily  <= 75                and
            _rsi_weekly  >  55                     and
            _dist_sma50  <= _sma50_threshold       and
            _delivery_ok
        )
    else:
        out["bk_trigger"] = None

    # ── breakout_setup ────────────────────────────────────────────────────
    _close_30d_bs  = out.get("close_30d")
    _above_sma50   = out.get("above_sma50")
    _sma50_gt_200  = out.get("sma50_gt_200")
    _wk_hi_high    = out.get("wk_hi_high")
    _wk_hi_low     = out.get("wk_hi_low")
    _vol_ratio_bs  = out.get("vol_ratio") or 0
    _rsi_daily_bs  = float(raw.get("rsi_daily")  or 0)
    _rsi_weekly_bs = float(raw.get("rsi_weekly") or 0)
    _consol_bs     = out.get("consol_range")
    _atr_pct_bs    = out.get("atr_pct") or 1.0

    if all(v is not None for v in [
        _close_30d_bs, _above_sma50, _sma50_gt_200,
        _wk_hi_high, _wk_hi_low, _consol_bs
    ]):
        _proximity_threshold = _close_30d_bs * (1 - min(0.03, _atr_pct_bs / 100))
        out["breakout_setup"] = bool(
            close          >= _proximity_threshold  and
            _above_sma50                            and
            _sma50_gt_200                           and
            _wk_hi_high    and _wk_hi_low           and
            _vol_ratio_bs  >= 1.2                   and
            55 <= _rsi_daily_bs  <= 75              and
            _rsi_weekly_bs >  50                    and
            _consol_bs     <  10
        )
    else:
        out["breakout_setup"] = None

    # ── Previously SHEET_ONLY — now from Supabase lookups ─────────────────
    ev = events.get(sym, {})
    out["upcoming_events"]     = ev.get("upcoming_events")
    out["upcoming_event_type"] = ev.get("upcoming_event_type")
    out["in_master_shortlist"] = bool(sym in msl_set)
    out["index_membership"]    = index_map.get(sym, "")
    out["company_name"]        = (company_map or {}).get(sym)

    # ── NOT_COMPUTABLE — explicit NULL ────────────────────────────────────
    out["fii_sector_flow"] = None

    # ── RENAME_MAP pass-throughs ──────────────────────────────────────────
    for chartink_col, canonical_col in RENAME_MAP.items():
        val = raw.get(chartink_col)
        if val is not None:
            out[canonical_col] = val

    # ── Same-name pass-throughs ───────────────────────────────────────────
    for field in PASSTHROUGH_FIELDS:
        if field not in out:
            val = raw.get(field)
            if val is not None:
                out[field] = val

    # ── Cast bigint fields (chartink may return decimals) ─────────────────
    BIGINT_FIELDS = {"volume", "market_cap", "avg_vol_20d", "avg_vol_50d"}
    for field in BIGINT_FIELDS:
        if out.get(field) is not None:
            try:
                out[field] = int(float(out[field]))
            except (TypeError, ValueError):
                pass

    return out


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════════════

def _numeric_diverged(c_val, s_val, field: str) -> tuple:
    """Returns (is_diverged, delta_pct)."""
    try:
        c_f, s_f  = float(c_val), float(s_val)
        threshold = WIDE_THRESHOLD if field in WIDE_TOLERANCE_FIELDS else DIVERGE_THRESHOLD
        delta     = abs(c_f - s_f) / abs(s_f) if s_f != 0 else (0.0 if c_f == 0 else 1.0)
        return delta > threshold, round(delta * 100, 2)
    except (TypeError, ValueError):
        return False, 0.0


def reconcile_row(computed: dict, sheet_row: dict,
                  reconcile_enabled: bool, field_trust: dict) -> tuple:
    """
    Merge computed with sheet_row using field-level trust levels.

    Trust levels:
      RECONCILE      — compare vs sheet; use computed if within tolerance,
                       sheet if diverged (calibration phase behaviour).
      COMPUTE_ALWAYS — always use computed regardless of sheet value.
      SHEET_ALWAYS   — always use sheet regardless of computed value.

    When reconcile_enabled=False: all fields treated as COMPUTE_ALWAYS.
    """
    if not reconcile_enabled:
        return computed, {"mode": "compute_only"}

    final = dict(sheet_row)
    meta  = {
        "mode":           "hybrid",
        "compute_always": [],
        "computed_match": [],
        "computed_only":  [],
        "sheet_always":   [],
        "diverged":       {},
    }

    computable = {
        k: v for k, v in computed.items()
        if k not in OTHER_SCRIPT_FIELDS
    }

    for field, c_val in computable.items():
        trust = field_trust.get(field, "RECONCILE")
        s_val = sheet_row.get(field)

        if trust == "COMPUTE_ALWAYS":
            final[field] = c_val
            meta["compute_always"].append(field)
            continue

        if trust == "SHEET_ALWAYS":
            if s_val is not None:
                final[field] = s_val
                meta["sheet_always"].append(field)
            else:
                final[field] = c_val
                meta["computed_only"].append(field)
            continue

        # RECONCILE (default) ─────────────────────────────────────────────
        if s_val is None:
            final[field] = c_val
            if c_val is not None:
                meta["computed_only"].append(field)
            continue

        if c_val is None and s_val is None:
            continue

        if c_val is None:
            final[field] = s_val
            continue

        # Boolean
        if isinstance(c_val, bool) or isinstance(s_val, bool):
            c_b, s_b = bool(c_val), bool(s_val)
            if c_b == s_b:
                final[field] = c_val
                meta["computed_match"].append(field)
            else:
                final[field] = s_val
                meta["diverged"][field] = {
                    "computed": c_b, "sheet": s_b,
                    "delta_pct": 100.0, "type": "bool_mismatch",
                    "trust": trust,
                    "action": "investigate — field formula may differ",
                }
            continue

        # Numeric
        try:
            diverged, delta_pct = _numeric_diverged(c_val, s_val, field)
            if not diverged:
                final[field] = c_val
                meta["computed_match"].append(field)
            else:
                final[field] = s_val
                meta["diverged"][field] = {
                    "computed":  round(float(c_val), 4),
                    "sheet":     round(float(s_val), 4),
                    "delta_pct": delta_pct,
                    "trust":     trust,
                    "action": (
                        f"investigate compute formula — delta {delta_pct:.1f}% "
                        f"({'wide' if field in WIDE_TOLERANCE_FIELDS else 'normal'} tolerance)"
                    ),
                }
            continue
        except (TypeError, ValueError):
            pass

        # String / mixed
        final[field] = c_val if c_val is not None else s_val
        if c_val is not None:
            meta["computed_match"].append(field)

    meta["summary"] = {
        "compute_always": len(meta["compute_always"]),
        "computed_match": len(meta["computed_match"]),
        "computed_only":  len(meta["computed_only"]),
        "sheet_always":   len(meta["sheet_always"]),
        "diverged":       len(meta["diverged"]),
    }
    return final, meta


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_last_trading_day(sb, reference_date: str, max_lookback: int = 10) -> str:
    """
    Walk backwards from reference_date until we find a date that is:
      - Not a Saturday/Sunday
      - Not in nse_holidays table
      - Has actual data in chartink_raw_data
    Raises RuntimeError if no valid date found within max_lookback days.
    """
    holiday_rows = sb.table("nse_holidays").select("date").execute().data
    holidays     = {row["date"] for row in holiday_rows}
    candidate    = datetime.strptime(reference_date, "%Y-%m-%d").date()

    for _ in range(max_lookback):
        date_str = str(candidate)
        if candidate.weekday() in (5, 6):
            logger.debug(f"  {date_str} is a weekend — stepping back")
            candidate -= timedelta(days=1)
            continue
        if date_str in holidays:
            logger.debug(f"  {date_str} is an NSE holiday — stepping back")
            candidate -= timedelta(days=1)
            continue
        probe = (sb.table("chartink_raw_data")
                   .select("date")
                   .eq("date", date_str)
                   .limit(1)
                   .execute().data)
        if probe:
            if date_str != reference_date:
                logger.info(f"  Resolved trading date: {reference_date} → {date_str}")
            return date_str
        logger.debug(f"  No data for {date_str} — stepping back")
        candidate -= timedelta(days=1)

    raise RuntimeError(
        f"No trading day with data found within {max_lookback} days before {reference_date}"
    )


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — compute_indicators skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())

    reconcile_enabled = cfg_bool("compute_indicators_reconcile", True)
    mode_label        = "HYBRID" if reconcile_enabled else "COMPUTE_ONLY"
    logger.info(
        f"compute_indicators v2.2 — {today} — {mode_label}"
        + (" [DRY RUN]" if DRY_RUN else "")
    )

    # ── Resolve last valid trading day ────────────────────────────────────
    try:
        today = resolve_last_trading_day(sb, today)
    except RuntimeError as exc:
        logger.error(str(exc))
        return {"status": "no_data"}

    # ── 6 bulk queries — no per-symbol calls ──────────────────────────────
    logger.info("Pass 1: bulk data fetch...")
    raw_rows = fetch_all_raw(sb, today)
    if not raw_rows:
        logger.warning(f"No chartink_raw_data for {today}")
        return {"status": "no_data"}

    sheet_map              = fetch_sheet_values(sb, today) if reconcile_enabled else {}
    symbols                = [r["symbol"] for r in raw_rows if r.get("symbol")]
    bulk_history           = fetch_bulk_history_yf(sb, today, symbols, dry_run=DRY_RUN)
    events_map             = fetch_upcoming_events(sb, today)
    msl_set                = fetch_msl_symbols(sb, today)
    index_map, company_map = fetch_index_membership(sb)
    nifty_ret              = fetch_nifty_return(sb, today)
    prices_map             = fetch_raw_prices(sb, today)
    field_trust            = fetch_field_trust(sb) if reconcile_enabled else {}

    logger.info(f"  Nifty 1M return: {nifty_ret}%")
    if reconcile_enabled and field_trust:
        overridden = {f: t for f, t in field_trust.items() if t != "RECONCILE"}
        if overridden:
            logger.info(f"  Field trust overrides: {overridden}")

    # ── Compute + reconcile per symbol ────────────────────────────────────
    logger.info("Pass 2: computing and reconciling...")
    upsert_rows       = []
    skipped           = 0
    total_always      = total_match = total_only = total_sheet_always = 0
    all_diverged: dict = defaultdict(list)

    for raw in raw_rows:
        sym = raw.get("symbol")
        if not sym:
            skipped += 1
            continue
        try:
            computed = compute_from_raw(
                raw, bulk_history.get(sym, []),
                events_map, msl_set, index_map, company_map,
                sheet_row=sheet_map.get(sym, {}),
                price_row=prices_map.get(sym, {}),
            )
            # Level 3 — market-relative (needs separate nifty scalar)
            if nifty_ret is not None and computed.get("ret_1m") is not None:
                computed["rs_vs_nifty"] = round(computed["ret_1m"] - nifty_ret, 4)

            computed["date"]   = today
            computed["symbol"] = sym

            final, meta = reconcile_row(
                computed, sheet_map.get(sym, {}),
                reconcile_enabled, field_trust,
            )
            final["compute_meta"] = json.dumps({
                "computed_at": datetime.now(IST).isoformat(),
                **meta,
            })[:4000]

            upsert_rows.append(final)

            s = meta.get("summary", {})
            total_always       += s.get("compute_always", 0)
            total_match        += s.get("computed_match", 0)
            total_only         += s.get("computed_only",  0)
            total_sheet_always += s.get("sheet_always",   0)
            for field, div in meta.get("diverged", {}).items():
                if isinstance(div, dict) and "delta_pct" in div:
                    all_diverged[field].append(div["delta_pct"])

        except Exception as exc:
            logger.warning(f"{sym}: failed — {exc}")
            skipped += 1

    # ── Reconciliation summary ────────────────────────────────────────────
    n = len(upsert_rows)
    logger.info("=" * 65)
    logger.info(f"COMPUTE INDICATORS SUMMARY — {today} — {mode_label}")
    logger.info(f"  Symbols: {n} processed | {skipped} skipped")
    if reconcile_enabled:
        logger.info(f"  COMPUTE_ALWAYS (verified fields):     {total_always:>6} decisions")
        logger.info(f"  COMPUTED_MATCH (within tolerance):    {total_match:>6} decisions")
        logger.info(f"  COMPUTED_ONLY  (sheet was NULL):      {total_only:>6} decisions")
        logger.info(f"  SHEET_ALWAYS   (confirmed diff basis):{total_sheet_always:>6} decisions")
        if all_diverged:
            logger.info(f"  DIVERGED (using sheet, needs review): {len(all_diverged)} field types")
            logger.info(f"  {'Field':<30} {'Avg delta':>10}  {'Symbols':>8}  Action")
            logger.info(f"  {'-'*65}")
            for field, deltas in sorted(
                all_diverged.items(), key=lambda x: -sum(x[1]) / len(x[1])
            ):
                avg = sum(deltas) / len(deltas)
                logger.info(
                    f"  {field:<30} {avg:>9.1f}%  {len(deltas):>8}  "
                    f"{'→ set SHEET_ALWAYS' if avg > 15 else '→ investigate formula'}"
                )
            logger.info("")
            logger.info("  To graduate a verified field to COMPUTE_ALWAYS:")
            logger.info("  UPDATE system_config SET value='COMPUTE_ALWAYS'")
            logger.info("    WHERE key='compute_trust_<field_name>';")
        else:
            logger.info("  No diverged fields — all computed values match sheet ✅")
    logger.info("=" * 65)

    if DRY_RUN:
        if upsert_rows:
            m0   = json.loads(upsert_rows[0].get("compute_meta", "{}"))
            smry = m0.get("summary", {})
            logger.info(
                f"[DRY RUN] Sample {upsert_rows[0]['symbol']}: "
                f"always={smry.get('compute_always')}, "
                f"match={smry.get('computed_match')}, "
                f"only={smry.get('computed_only')}, "
                f"diverged={smry.get('diverged')}"
            )
        return {"computed": n, "skipped": skipped, "dry_run": True}

    # ── Batch upsert — 50 rows per call ──────────────────────────────────
    logger.info(f"Pass 3: writing {n} rows to stock_data_daily...")
    written = 0
    for i in range(0, len(upsert_rows), 50):
        sb.table("stock_data_daily").upsert(
            upsert_rows[i:i + 50], on_conflict="date,symbol"
        ).execute()
        written += len(upsert_rows[i:i + 50])

    logger.success(
        f"compute_indicators: {written} written | {skipped} skipped | "
        f"{len(all_diverged)} diverged field types requiring review"
    )
    return {
        "status":          "ok",
        "computed":        written,
        "skipped":         skipped,
        "diverged_fields": len(all_diverged),
        "mode":            mode_label,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Compute Indicators v2.2")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="Skip sheet reconciliation — use computed everywhere")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    if args.no_reconcile:
        os.environ["COMPUTE_INDICATORS_RECONCILE"] = "false"
    print(main())