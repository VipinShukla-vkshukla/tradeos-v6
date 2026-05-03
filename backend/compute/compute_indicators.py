"""
TradeOS v6 — Phase 2: Compute Indicators v2.3
==============================================
Hybrid reconciliation with field-level trust graduation.

CHANGE LOG v2.2 → v2.3
  - Replaced single MIN_SESSIONS_REQUIRED with two purpose-specific constants:
      MIN_SESSIONS_FULL_FETCH  = 0   — only brand-new symbols get a full pull
      MIN_SESSIONS_FOR_RETURNS = 260 — quality threshold; NOT a fetch trigger
  - Added 3rd classification bucket: need_yf_gap
      count == 0              → need_yf_full  (new symbol, full pull)
      0 < count < 260         → need_yf_gap   (partial history, backfill to cutoff)
      count >= 260, stale     → need_yf_tail  (sufficient, short tail refresh)
      count >= 260, current   → sufficient    (nothing needed)
  - Fixed sym_cached_up_to bug: gap/full symbols now write ALL fetched rows;
    tail symbols still write only rows newer than their latest cached date.
    upsert on_conflict="symbol,date" deduplicates safely.
  - Removed duplicate list declarations (dead code).
  - Removed unused gap_sym_starts dict (dead code).
  - Moved low-session warning into need_yf_gap branch where it is reachable.
  - Fixed yfinance log line to report full + gap + tail counts correctly.
  - Fixed seed log to reference correct constant (was still using removed
    MIN_SESSIONS_REQUIRED — would have raised NameError).
  - days_back=370 + 100-day buffer = 470 calendar days ≈ 335 trading sessions,
    sufficient for ret_12m (needs closes[240]).

DESIGN OVERVIEW
  Runs AFTER ingest_sheets.py. Sheet values are already in stock_data_daily.
  Script computes from chartink_raw_data + Supabase lookup tables, then
  reconciles field-by-field using per-field trust levels from system_config.

HISTORICAL RETURNS — HOW THEY ARE POPULATED
  All return fields are sourced from price_history_yf (cached OHLCV).
  yfinance is ONLY called when:
    a) A symbol has zero cached rows (brand new)
    b) A symbol has < 260 rows (partial history — backfill to cutoff)
    c) A symbol's latest cached date is behind today (tail refresh)
  On normal trading days after initial load, yfinance is NOT called.

  Fields populated from history:
    ret_1w        — closes[5]   (1 week back)
    ret_1m        — closes[20]  (1 month back)
    ret_3m        — closes[60]  (3 months back)
    ret_6m        — closes[120] (6 months back)
    ret_12m       — closes[240] (12 months back)
    price_6m_ago  — closes[120]
    price_12m_ago — closes[240]
    close_30d     — closes[30]
    high_52w      — max(highs[:252])
    low_52w       — min(lows[:252])
    high_30d      — max(highs[:30])
    low_30d       — min(lows[:30])
    consol_range  — derived from high_30d / low_30d
    wk_hi_high    — recent vs prior 5-session highs
    wk_hi_low     — recent vs prior 5-session lows
    bk_trigger    — composite breakout signal
    breakout_setup — proximity + trend + momentum signal

FIELD TRUST LEVELS (stored in system_config, key = compute_trust_<field>)
  RECONCILE       — compare vs sheet, use computed if within tolerance,
                    use sheet if diverged, log divergence for investigation
  COMPUTE_ALWAYS  — ignore sheet, always use computed (field is verified)
  SHEET_ALWAYS    — always use sheet, do not overwrite (field is different basis)

FIELDS GENUINELY NOT COMPUTABLE
  fii_sector_flow — sector-level FII requires paid data (Bloomberg etc.)
                    Set to NULL explicitly. Do not source from Sheet.

PERFORMANCE
  6 bulk queries total for all 500 symbols (not 1000 per-symbol like v1):
    1. chartink_raw_data today
    2. stock_data_daily today (sheet baseline)
    3. price_history_yf cache (chunked, 20 symbols at a time)
    4. nifty_upcoming_events (next 30 days)
    5. master_shortlist today (in_master_shortlist flag)
    6. nifty_total_market (index_membership — static, cached)
    + 1 scalar: market_regime for nifty return
  yfinance called ONLY for new / partial / stale symbols.
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

# ── Cache session thresholds ──────────────────────────────────────────────────
MIN_SESSIONS_FULL_FETCH  = 0    # Symbols with ZERO cached rows → full yfinance pull.
                                 # Any symbol with even 1 row is extended by tail/gap only.

MIN_SESSIONS_FOR_RETURNS = 260  # Below this, ret_6m / ret_12m degrade gracefully to None.
                                 # Triggers a gap-fill fetch to backfill history.
                                 # NOT a full-refetch trigger — existing rows are preserved.
                                 # 260 sessions ≈ 12 months of trading days.

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
    "pct_change":     "pct_change",
}

PASSTHROUGH_FIELDS = [
    "high_30d", "sma_10", "sma_20", "sma_50", "sma_200",
    "ema_10", "ema_20", "ema_50", "rsi_daily", "rsi_weekly", "rsi_monthly",
    "volume", "vwap_20d", "vwap_50d", "atr_14",
    "ha_high", "ha_low", "ha_close", "supertrend",
    "macd_line", "macd_signal", "ttm_net_profit", "eps",
    "market_cap", "sector", "industry", "company_name",
]

OTHER_SCRIPT_FIELDS = {
    "asm_flag",         # ingest_asm_gsm.py
    "fo_ban_flag",      # ingest_asm_gsm.py
    "kite_price",       # kite_reconcile.py
    "predicted_regime", # ml_regime_predict.py
    "delivery_pct",     # ingest_bhavcopy.py
    "delivery_qty",     # ingest_bhavcopy.py
    "value_cr",         # ingest_bhavcopy.py
}

NOT_COMPUTABLE_FIELDS = {
    "fii_sector_flow",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: BULK HISTORY — price_history_yf cache + yfinance fallback
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_bulk_history_yf(sb, today: str, symbols: list,
                           days_back: int = 370,
                           cache_to_db: bool = True,
                           dry_run: bool = False,
                           today_ohlcv: dict | None = None) -> dict:
    """
    Fetch OHLC history for all symbols.

    Data priority:
      1. price_history_yf (Supabase cache) — always queried first
      2. yfinance — called ONLY for:
           a) new symbols with zero cached rows    (need_yf_full)
           b) symbols with < 260 cached sessions  (need_yf_gap — backfill)
           c) symbols with stale latest date      (need_yf_tail — short refresh)

    Args:
        sb          : Supabase client
        today       : last trading day YYYY-MM-DD (already resolved by caller)
        symbols     : list of NSE symbol strings
        days_back   : calendar days of history window (cutoff = days_back + 100)
        cache_to_db : if True, saves yfinance results to price_history_yf
        dry_run     : if True, skips all writes
        today_ohlcv : {symbol: {open,high,low,close,volume}} from chartink_raw_data

    Returns:
        {symbol: [{close, low, high, date}, ...]} newest first
    """
    # cutoff = 470 calendar days ≈ 335 trading sessions (covers ret_12m = 240 sessions)
    cutoff   = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days_back + 100)).date())
    result   = {}

    # ── Step 1: Lightweight sufficiency check (symbol + date only) ────────
    logger.info(
        f"  fetch_bulk_history_yf: checking price_history_yf cache "
        f"({len(symbols)} symbols) | cutoff={cutoff}..."
    )

    sym_counts: dict = {}   # {symbol: row_count within cutoff window}
    sym_latest: dict = {}   # {symbol: most_recent_date_str}

    summary_rows = sb.rpc(
        "get_symbol_history_summary",
        {"p_symbols": symbols, "p_cutoff": cutoff, "p_today": today}
    ).execute().data

    sym_counts = {r["symbol"]: r["row_count"]   for r in summary_rows if r.get("symbol")}
    sym_latest = {r["symbol"]: r["latest_date"] for r in summary_rows if r.get("symbol")}
    sym_first  = {r["symbol"]: r["first_date"]  for r in summary_rows if r.get("symbol")}

    # ── Step 1b: Seed today's OHLCV from chartink into cache ─────────────
    # Avoids tail-refresh calls for symbols that are already current.
    # chartink captured today's OHLCV — writing it here sets
    # sym_latest[sym] == today so classification skips tail entirely.
    new_sym_count = len([s for s in symbols if sym_counts.get(s, 0) == 0])

    if today_ohlcv:
        seed_rows = [
            {
                "symbol": sym,
                "date":   today,
                "open":   ohlcv.get("open"),
                "high":   ohlcv.get("high"),
                "low":    ohlcv.get("low"),
                "close":  ohlcv.get("close"),
                "volume": ohlcv.get("volume"),
            }
            for sym, ohlcv in today_ohlcv.items()
            if ohlcv.get("close")
        ]
        if cache_to_db and not dry_run and seed_rows:
            for i in range(0, len(seed_rows), 500):
                sb.table("price_history_yf").upsert(
                    seed_rows[i:i + 500], on_conflict="symbol,date"
                ).execute()
        # Update sym_latest in memory so classification sees latest == today.
        # Do NOT update sym_counts — counts must reflect pre-seed state
        # so classification correctly identifies gap/full symbols.
        for sym in today_ohlcv:
            if today_ohlcv[sym].get("close"):
                sym_latest[sym] = today

        logger.info(
            f"  Today OHLCV seeded from chartink: {len(seed_rows)} symbols"
            f" | tail calls eliminated for current symbols"
            + (f" | {new_sym_count} brand-new symbols still need full yfinance fetch"
               if new_sym_count else "")
        )

# ── Step 2: Classify symbols into 5 buckets ───────────────────────────
    #
    #   need_yf_full   : count == 0               → brand new, no history at all
    #   need_yf_gap    : cache genuinely incomplete→ gap vs expected sessions > 15%
    #   ipo_syms       : count < 260 but cache is  → young listing, cache complete
    #                    complete for its age         for its age, skip yfinance
    #   need_yf_tail   : count >= 260, stale       → short tail refresh
    #   sufficient     : count >= 260, current     → nothing needed
    #
    need_yf_full:    list = []
    need_yf_gap:     list = []
    need_yf_tail:    list = []
    sufficient_syms: list = []
    ipo_syms:        list = []   # young but cache-complete

    ipo_meta: dict = {}
    for sym in symbols:
        count  = sym_counts.get(sym, 0)
        latest = sym_latest.get(sym, today)
        first  = sym_first.get(sym)          # MIN(date) from RPC — None if no rows

        if count == 0:
            # Genuinely new symbol — no history at all
            need_yf_full.append(sym)
            continue

        # ── Is cache complete relative to this symbol's actual age? ──────
        # For established symbols: first ≈ cutoff date (Jan 2025)
        #   → expected ≈ 335 sessions, completeness check is strict
        # For IPOs: first = their actual listing date (e.g. Oct 2025)
        #   → expected ≈ 120 sessions, so count/expected ≈ 96% → cache complete
        if first:
            d1 = datetime.strptime(str(first), "%Y-%m-%d").date()
            d2 = datetime.strptime(today,      "%Y-%m-%d").date()
            expected = max(1, int((d2 - d1).days * (5 / 7) * 0.96))
            cache_completeness = count / expected
            is_cache_complete  = cache_completeness >= 0.85
        else:
            expected           = MIN_SESSIONS_FOR_RETURNS
            cache_completeness = 0.0
            is_cache_complete  = False

        if count < MIN_SESSIONS_FOR_RETURNS:
            if is_cache_complete:
                # Young listing — cache is as full as it can be, skip yfinance
                ipo_syms.append(sym)
                # Store metadata for summary log below
                ipo_meta[sym] = {
                    "first_date":   str(first),
                    "count":        count,
                    "expected":     expected,
                    "completeness": cache_completeness,
                }
                logger.debug(
                    f"  {sym}: young listing — {count}/{expected} expected sessions "
                    f"({cache_completeness:.0%}) | ret_6m/ret_12m will be None until matured"
                )
            else:
                # Genuine gap — cache is missing data that should exist
                need_yf_gap.append(sym)
                logger.warning(
                    f"  {sym}: cache gap — {count}/{expected} expected sessions "
                    f"({cache_completeness:.0%}) — gap-fill triggered back to {cutoff}"
                )
            continue

        # Sufficient history (count >= 260)
        sufficient_syms.append(sym)
        if latest < today:
            need_yf_tail.append(sym)
        # else: current and sufficient — nothing needed

    # ── IPO symbols summary ───────────────────────────────────────────────
    if ipo_syms:
        logger.info(f"  Young listings skipping yfinance ({len(ipo_syms)} symbols):")
        logger.info(f"  {'Symbol':<16} {'Listed Since':<14} {'Cached':>7} {'Expected':>9} {'Complete':>9}  Reason")
        logger.info(f"  {'-'*70}")
        for sym in sorted(ipo_syms):
            m = ipo_meta[sym]
            # Compute how many more sessions until graduation to sufficient
            sessions_remaining = MIN_SESSIONS_FOR_RETURNS - m["count"]
            # Approximate calendar days remaining (reverse of trading day formula)
            days_remaining = int(sessions_remaining / (5 / 7) / 0.96)
            reason = (
                f"needs {sessions_remaining} more sessions "
                f"(~{days_remaining}d) to reach {MIN_SESSIONS_FOR_RETURNS}"
            )
            logger.info(
                f"  {sym:<16} {m['first_date']:<14} "
                f"{m['count']:>7} {m['expected']:>9} "
                f"{m['completeness']:>8.0%}  {reason}"
            )
        logger.info(f"  {'-'*70}")
    
    need_yf_backfill = need_yf_full + need_yf_gap   # both use full cutoff as start
    need_yf          = need_yf_backfill + need_yf_tail

    logger.info(
        f"  price_history_yf: {len(sufficient_syms)} sufficient"
        f" | {len(need_yf_tail)} tail-refresh"
        f" | {len(need_yf_gap)} gap-fill (genuine cache gaps)"
        f" | {len(ipo_syms)} young listings (cache complete, skipping yfinance)"
        f" | {len(need_yf_full)} full fetch (new symbols)"
        + ("\n  ⚠ Cold start: all symbols have zero cached history — "
           "full yfinance pull required"
           if len(need_yf_full) == len(symbols) else "")
        + ("\n  ✓ yfinance not needed — all symbols sufficient and current"
           if not need_yf else "")
    )

    # ── Step 3: Load full OHLCV from cache ────────────────────────────────
    # Covers both sufficient symbols (260+ sessions) and IPO symbols
    # (young but cache-complete). Neither needs yfinance.
    # Chunk = 3 symbols × ~335 rows ≈ 1005 rows — stays under Supabase 1000
    # row default. Paginate with .range() to handle any symbol safely.
    OHLCV_CHUNK = 3
    syms_to_load = sufficient_syms + ipo_syms   # ipo_syms served from cache

    for i in range(0, len(syms_to_load), OHLCV_CHUNK):
        chunk = syms_to_load[i:i + OHLCV_CHUNK]
        offset = 0
        while True:
            rows = (sb.table("price_history_yf")
                      .select("symbol,date,open,high,low,close,volume")
                      .in_("symbol", chunk)
                      .gte("date", cutoff)
                      .lte("date", today)
                      .order("date", desc=True)
                      .range(offset, offset + 999)
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
            if len(rows) < 1000:
                break        # last page
            offset += 1000

    # ── Step 4: yfinance for new / gap / stale symbols ────────────────────
    if need_yf and YF_AVAILABLE:
        logger.info(
            f"  yfinance: {len(need_yf_full)} new symbol"
            f" + {len(need_yf_gap)} gap-fill"
            f" + {len(need_yf_tail)} tail fetches..."
        )

        end_date = str((datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).date())

        # Tail cutoff: dynamic — covers multi-day gaps from failures/holidays/weekends.
        # Uses oldest sym_latest among tail symbols as anchor, caps at 60 days.
        if need_yf_tail:
            oldest_cached = min(sym_latest.get(s, today) for s in need_yf_tail)
            gap_days = (datetime.strptime(today, "%Y-%m-%d").date() -
                        datetime.strptime(oldest_cached, "%Y-%m-%d").date()).days
            tail_days = min(max(gap_days + 1, 3), 60)
        else:
            tail_days = 7
        tail_cutoff = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=tail_days)).date())

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

        # Backfill (full + gap) uses global cutoff; tail uses short tail_cutoff
        batches = [
            (need_yf_backfill, *_download_batch(need_yf_backfill, cutoff)),
            (need_yf_tail,     *_download_batch(need_yf_tail,     tail_cutoff)),
        ]

        rows_to_cache: list    = []
        need_yf_backfill_set   = set(need_yf_backfill)   # O(1) lookup inside loop

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

                    # ── KEY FIX: write threshold per bucket ───────────────
                    # Backfill (full + gap): write ALL rows — "1970-01-01" means
                    #   nothing is excluded. upsert on symbol,date deduplicates.
                    # Tail: write only rows NEWER than the latest cached date
                    #   to avoid unnecessary upsert overhead.
                    write_from = (
                        "1970-01-01"
                        if sym in need_yf_backfill_set
                        else sym_latest.get(sym, "1970-01-01")
                    )

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

                        if cache_to_db and not dry_run and date_str > write_from:
                            rows_to_cache.append({
                                "symbol": sym,
                                "date":   date_str,
                                "open":   round(float(row["Open"]), 4),
                                "high":   entry["high"],
                                "low":    entry["low"],
                                "close":  entry["close"],
                                "volume": int(row["Volume"]) if row.get("Volume") else None,
                            })

                    # Tail-refresh: merge new rows into existing cached result
                    if sym in result:
                        existing_dates = {r["date"] for r in result[sym]}
                        for h in history:
                            if h["date"] not in existing_dates:
                                result[sym].insert(0, h)
                        result[sym].sort(key=lambda x: x["date"], reverse=True)
                    else:
                        result[sym] = history

                    logger.debug(f"  yfinance: {sym} — {len(history)} sessions fetched")

                except Exception as exc:
                    logger.warning(f"  yfinance: {sym} failed — {exc}")

        # ── Step 5: Write-through cache ───────────────────────────────────
        if cache_to_db and not dry_run and rows_to_cache:
            logger.info(f"  Caching {len(rows_to_cache)} new rows to price_history_yf...")
            for i in range(0, len(rows_to_cache), 500):
                sb.table("price_history_yf").upsert(
                    rows_to_cache[i:i + 500],
                    on_conflict="symbol,date"
                ).execute()
            logger.info(f"  Cached successfully — {len(rows_to_cache)} rows written")
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
        f"  fetch_bulk_history_yf complete: {len(result)} symbols loaded"
        f" | {total_sessions} total sessions"
        f" | {yf_fetched} symbols fetched from yfinance"
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
    {symbol: {upcoming_events, upcoming_event_type}} for next window_days.
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


def fetch_raw_prices(sb, today: str, symbols: list | None = None) -> dict:
    query = (sb.table("raw_prices")
               .select("symbol,value_cr,delivery_pct,delivery_qty")
               .eq("date", today))
    if symbols:
        query = query.in_("symbol", symbols)
    rows = query.execute().data
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

    sym_history: [{close, low, high, date}] newest first — sourced entirely
    from price_history_yf. All return fields, rolling highs/lows, and
    breakout signals are derived from this list.

    Return field session requirements:
      ret_1w        closes[5]    ~1 week
      ret_1m        closes[20]   ~1 month
      ret_3m        closes[60]   ~3 months
      ret_6m        closes[120]  ~6 months
      ret_12m       closes[240]  ~12 months
      price_6m_ago  closes[120]
      price_12m_ago closes[240]
      close_30d     closes[30]
    All return None gracefully if history is insufficient.
    """
    out: dict = {}
    sym = raw.get("symbol", "")

    # ── Raw values from chartink ──────────────────────────────────────────
    close       = float(raw.get("daily_close") or 0)
    volume      = float(raw.get("volume")       or 0)
    avg_vol_20  = float(raw.get("avg_vol_20")   or 0)
    atr_14      = float(raw.get("atr_14")       or 0)
    sma_50      = float(raw.get("sma_50")       or 0)
    _sma200_raw = raw.get("sma_200")
    sma_200     = float(_sma200_raw) if _sma200_raw not in (None, "", 0) else 0.0
    if _sma200_raw not in (None, "", 0):
        out["sma_200"] = round(float(_sma200_raw), 4)
    supertrend  = float(raw.get("supertrend")   or 0)

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
        out["sma_200"]  = round(sma_200, 4)
    out["above_st"]     = bool(close > supertrend) if supertrend > 0 else None
    if week52_hi > week52_lo > 0:
        out["price_location"] = round(
            (close - week52_lo) / (week52_hi - week52_lo) * 100, 2
        )

    # ── Level 2 — Historical return series ───────────────────────────────
    # All sourced from price_history_yf via sym_history (newest first).
    # _ret(n) returns None gracefully when insufficient sessions exist.
    closes = [h["close"] for h in sym_history]

    def _ret(n: int) -> float | None:
        """Return % change vs n sessions ago. None if history insufficient."""
        if len(closes) > n and closes[n] not in (None, 0):
            return round((close - closes[n]) / closes[n] * 100, 4)
        return None

    out["ret_1w"]        = _ret(5)     # ~1 week   (5 trading sessions)
    out["ret_1m"]        = _ret(20)    # ~1 month  (20 trading sessions)
    out["ret_3m"]        = _ret(60)    # ~3 months (60 trading sessions)
    out["ret_6m"]        = _ret(120)   # ~6 months (120 trading sessions)
    out["ret_12m"]       = _ret(240)   # ~12 months (240 trading sessions)
    out["price_6m_ago"]  = round(closes[120], 2) if len(closes) > 120 else None
    out["price_12m_ago"] = round(closes[240], 2) if len(closes) > 240 else None
    out["close_30d"]     = round(closes[30],  2) if len(closes) > 30  else None

    # ── 30-day rolling high / low / consolidation range ───────────────────
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

    # ── wk_hi_high / wk_hi_low — recent 5 sessions vs prior 5 ───────────
    window_10 = sym_history[:15]
    highs_10  = [h["high"] for h in window_10 if h.get("high") is not None][:10]
    lows_10   = [h["low"]  for h in window_10 if h.get("low")  is not None][:10]

    out["wk_hi_high"] = (
        bool(max(highs_10[:5]) > max(highs_10[5:10])) if len(highs_10) >= 10 else None
    )
    out["wk_hi_low"] = (
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
            close        >  _high_30d          and
            close        >  _close_30d         and
            _vol_ratio   >= 1.5                and
            _above_st                          and
            (_open == 0 or close > _open)      and
            55 <= _rsi_daily  <= 75            and
            _rsi_weekly  >  55                 and
            _dist_sma50  <= _sma50_threshold   and
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
            close          >= _proximity_threshold and
            _above_sma50                           and
            _sma50_gt_200                          and
            _wk_hi_high    and _wk_hi_low          and
            _vol_ratio_bs  >= 1.2                  and
            55 <= _rsi_daily_bs  <= 75             and
            _rsi_weekly_bs >  50                   and
            _consol_bs     <  10
        )
    else:
        out["breakout_setup"] = None

    # ── Previously SHEET_ONLY — now computed from Supabase lookups ────────
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

    # ── Cast bigint fields (chartink may return float) ────────────────────
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
                       sheet if diverged (calibration phase).
      COMPUTE_ALWAYS — always use computed (field verified).
      SHEET_ALWAYS   — always use sheet (field uses different formula/basis).

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

        # Boolean comparison
        if isinstance(c_val, bool) or isinstance(s_val, bool):
            c_b, s_b = bool(c_val), bool(s_val)
            if c_b == s_b:
                final[field] = c_val
                meta["computed_match"].append(field)
            else:
                final[field] = s_val
                meta["diverged"][field] = {
                    "computed":  c_b,
                    "sheet":     s_b,
                    "delta_pct": 100.0,
                    "type":      "bool_mismatch",
                    "trust":     trust,
                    "action":    "investigate — field formula may differ",
                }
            continue

        # Numeric comparison
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
    Walk backwards from reference_date to find the last day that:
      - Is not Saturday/Sunday
      - Is not in nse_holidays table
      - Has actual data in chartink_raw_data
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
        logger.debug(f"  No chartink data for {date_str} — stepping back")
        candidate -= timedelta(days=1)

    raise RuntimeError(
        f"No trading day with data found within {max_lookback} days before {reference_date}"
    )

def expected_trading_sessions(from_date: str, to_date: str) -> int:
    """
    Approximate trading sessions between two dates.
    Formula: calendar days × (5/7) × 0.96
      5/7  = removes weekends
      0.96 = removes ~4% for public holidays (NSE averages ~10-12/year)
    """
    d1 = datetime.strptime(from_date, "%Y-%m-%d").date()
    d2 = datetime.strptime(to_date,   "%Y-%m-%d").date()
    calendar_days = (d2 - d1).days
    return max(0, int(calendar_days * (5 / 7) * 0.96))

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — compute_indicators skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())

    reconcile_enabled = cfg_bool("compute_indicators_reconcile", True)
    mode_label        = "HYBRID" if reconcile_enabled else "COMPUTE_ONLY"
    logger.info(
        f"compute_indicators v2.3 — {today} — {mode_label}"
        + (" [DRY RUN]" if DRY_RUN else "")
    )

    # ── Resolve last valid trading day ────────────────────────────────────
    try:
        today = resolve_last_trading_day(sb, today)
    except RuntimeError as exc:
        logger.error(str(exc))
        return {"status": "no_data"}

    # ── Pass 1: 6 bulk queries — no per-symbol DB calls ──────────────────
    logger.info("Pass 1: bulk data fetch...")
    raw_rows = fetch_all_raw(sb, today)
    if not raw_rows:
        logger.warning(f"No chartink_raw_data for {today}")
        return {"status": "no_data"}

    sheet_map = fetch_sheet_values(sb, today) if reconcile_enabled else {}
    symbols   = [r["symbol"] for r in raw_rows if r.get("symbol")]

    # Seed today's OHLCV into price_history_yf from chartink.
    # Makes sym_latest[sym] == today → skips tail-refresh for current symbols.
    today_ohlcv = {
        r["symbol"]: {
            "open":   r.get("daily_open"),
            "high":   r.get("daily_high"),
            "low":    r.get("daily_low"),
            "close":  r.get("daily_close"),
            "volume": r.get("volume"),
        }
        for r in raw_rows if r.get("symbol") and r.get("daily_close")
    }

    bulk_history           = fetch_bulk_history_yf(
                                sb, today, symbols,
                                dry_run=DRY_RUN,
                                today_ohlcv=today_ohlcv
                             )
    events_map             = fetch_upcoming_events(sb, today)
    msl_set                = fetch_msl_symbols(sb, today)
    index_map, company_map = fetch_index_membership(sb)
    nifty_ret              = fetch_nifty_return(sb, today)
    prices_map             = fetch_raw_prices(sb, today, symbols=symbols)
    field_trust            = fetch_field_trust(sb) if reconcile_enabled else {}

    logger.info(f"  Nifty 1M return: {nifty_ret}%")
    if reconcile_enabled and field_trust:
        overridden = {f: t for f, t in field_trust.items() if t != "RECONCILE"}
        if overridden:
            logger.info(f"  Field trust overrides: {overridden}")

    # ── Pass 2: Compute + reconcile per symbol ────────────────────────────
    logger.info("Pass 2: computing and reconciling...")
    upsert_rows        = []
    skipped            = 0
    total_always       = total_match = total_only = total_sheet_always = 0
    all_diverged: dict = defaultdict(list)

    for raw in raw_rows:
        sym = raw.get("symbol")
        if not sym:
            skipped += 1
            continue
        try:
            computed = compute_from_raw(
                raw,
                bulk_history.get(sym, []),
                events_map, msl_set, index_map, company_map,
                sheet_row=sheet_map.get(sym, {}),
                price_row=prices_map.get(sym, {}),
            )

            # Level 3 — market-relative (requires separate nifty scalar)
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
            logger.warning(f"{sym}: computation failed — {exc}")
            skipped += 1

    # ── Reconciliation summary ────────────────────────────────────────────
    n = len(upsert_rows)
    logger.info("=" * 65)
    logger.info(f"COMPUTE INDICATORS SUMMARY — {today} — {mode_label}")
    logger.info(f"  Symbols: {n} processed | {skipped} skipped")
    if reconcile_enabled:
        logger.info(f"  COMPUTE_ALWAYS (verified fields):      {total_always:>6} decisions")
        logger.info(f"  COMPUTED_MATCH (within tolerance):     {total_match:>6} decisions")
        logger.info(f"  COMPUTED_ONLY  (sheet was NULL):       {total_only:>6} decisions")
        logger.info(f"  SHEET_ALWAYS   (confirmed diff basis): {total_sheet_always:>6} decisions")
        if all_diverged:
            logger.info(f"  DIVERGED (using sheet, needs review):  {len(all_diverged)} field types")
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

    # ── Pass 3: Batch upsert — 50 rows per call ───────────────────────────
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
    parser = argparse.ArgumentParser(description="TradeOS v6 — Compute Indicators v2.3")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="Skip sheet reconciliation — use computed everywhere")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    if args.no_reconcile:
        os.environ["COMPUTE_INDICATORS_RECONCILE"] = "false"
    print(main())