"""
TradeOS v6 — Phase 2: Compute Indicators v2.1
==============================================
Hybrid reconciliation with field-level trust graduation.

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
  fetch_bulk_history() uses chartink_raw_data going back up to 370 days.
  On Day 1 of Phase 2 you may have < 120 sessions → ret_6m returns None.
  Reconciliation will use the Sheet value for those fields automatically
  until chartink_raw_data accumulates enough history (~6 months).
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
    3. chartink_raw_data history (370 days)
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

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, cfg_bool, cfg, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# ── Tolerance thresholds ──────────────────────────────────────────────────────
DIVERGE_THRESHOLD = 0.02   # 2% for most numeric fields
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


# ── Bulk data fetch — 6 queries for 500 symbols ───────────────────────────────

def fetch_all_raw(sb, today: str) -> list[dict]:
    rows = sb.table("chartink_raw_data").select("*").eq("date", today).execute().data
    logger.info(f"  chartink_raw_data: {len(rows)} rows")
    return rows


def fetch_sheet_values(sb, today: str) -> dict:
    """Read today's stock_data_daily (written by ingest_sheets). {symbol: row}."""
    rows = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    logger.info(f"  stock_data_daily (sheet baseline): {len(rows)} rows")
    return {r["symbol"]: r for r in rows}


def fetch_bulk_history(sb, today: str, days_back: int = 370) -> dict:
    """
    Bulk fetch of historical OHLC from chartink_raw_data.
    Returns {symbol: [{close, low, high}, ...]} newest first.

    Note on historical depth:
      ret_12m needs ~252 sessions. If chartink_raw_data has < 252 sessions
      (e.g. Phase 2 just activated), ret_12m will be None → reconciliation
      will automatically fall back to Sheet value for that field.
      This self-heals as data accumulates over 6-12 months.
      No code change needed.
    """
    cutoff = str((today_ist() - timedelta(days=days_back)).date())
    rows = (sb.table("chartink_raw_data")
              .select("symbol,date,daily_close,daily_low,daily_high")
              .lte("date", today)
              .gte("date", cutoff)
              .order("date", desc=True)
              .execute().data)
    history: dict = defaultdict(list)
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        history[sym].append({
            "close": float(r["daily_close"]) if r.get("daily_close") else None,
            "low":   float(r["daily_low"])   if r.get("daily_low")   else None,
            "high":  float(r["daily_high"])  if r.get("daily_high")  else None,
        })
    logger.info(f"  Bulk history: {len(rows)} rows across {len(history)} symbols")
    return dict(history)


def fetch_upcoming_events(sb, today: str, window_days: int = 14) -> dict:
    """
    {symbol: {events_str, event_type_str}} for next window_days.
    Maps upcoming_events and upcoming_event_type from nifty_upcoming_events table.
    Takes the nearest event per symbol. Previously SHEET_ONLY — now from Supabase.
    """
    cutoff = str((today_ist() + timedelta(days=window_days)).date())
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
            continue   # keep nearest event only
        purpose = r.get("purpose") or ""
        details = r.get("details") or purpose
        result[sym] = {
            "upcoming_events":      details[:200] if details else None,
            "upcoming_event_type":  purpose[:100] if purpose else None,
        }
    logger.info(f"  Upcoming events: {len(result)} symbols with events in next {window_days}d")
    return result


def fetch_msl_symbols(sb, today: str) -> set:
    """
    Set of symbols present in master_shortlist for today.
    Used to populate in_master_shortlist. Previously SHEET_ONLY — now from Supabase.
    """
    rows = (sb.table("master_shortlist")
              .select("symbol")
              .eq("date", today)
              .execute().data)
    if not rows:
        # Weekend/holiday fallback — use most recent available date
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


def fetch_index_membership(sb) -> dict:
    """
    {symbol: index_string} from nifty_total_market.
    Previously SHEET_ONLY — now from Supabase (static table, never changes mid-day).
    """
    rows = sb.table("nifty_total_market").select("symbol,nifty_200,nifty_500").execute().data
    result: dict = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        in_200 = bool(r.get("nifty_200"))
        in_500 = bool(r.get("nifty_500"))
        if in_200:
            result[sym] = "Nifty200,Nifty500"   # Nifty200 ⊂ Nifty500
        elif in_500:
            result[sym] = "Nifty500"
        else:
            result[sym] = ""
    logger.info(f"  Index membership: {len(result)} symbols mapped")
    return result


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
    except Exception as e:
        logger.debug(f"Nifty return failed: {e}")
    return None


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
        trust = {}
        for r in rows:
            field = r["key"].replace("compute_trust_", "")
            trust[field] = (r.get("value") or "RECONCILE").upper()
        return trust
    except Exception as e:
        logger.debug(f"Field trust fetch failed (using defaults): {e}")
        return {}


# ── Computation ────────────────────────────────────────────────────────────────

def compute_from_raw(raw: dict, sym_history: list, events: dict,
                     msl_set: set, index_map: dict) -> dict:
    """
    Compute all derivable fields for one symbol.
    sym_history: [{close, low, high}] newest first from bulk fetch.
    """
    out: dict = {}
    sym = raw.get("symbol", "")

    # ── Raw values (chartink field names) ─────────────────────────────────
    close      = float(raw.get("daily_close") or 0)
    volume     = float(raw.get("volume")       or 0)
    avg_vol_20 = float(raw.get("avg_vol_20")   or 0)
    atr_14     = float(raw.get("atr_14")       or 0)
    sma_50     = float(raw.get("sma_50")       or 0)
    sma_200    = float(raw.get("sma_200")      or 0)
    week52_hi  = float(raw.get("week52_high")  or 0)
    week52_lo  = float(raw.get("week52_low")   or 0)
    supertrend = float(raw.get("supertrend")   or 0)

    # ── Level 1 ───────────────────────────────────────────────────────────
    out["vol_ratio"]     = round(volume / avg_vol_20, 4) if avg_vol_20 > 0 else None
    out["atr_pct"]       = round(atr_14 / close * 100, 4) if close > 0 else None
    out["current_price"] = close if close > 0 else None   # v1 BUG FIX

    # ── Level 2 — SMA / price flags ───────────────────────────────────────
    out["dist_sma50"]   = round((close - sma_50)  / sma_50  * 100, 4) if sma_50  > 0 else None
    out["above_sma50"]  = bool(close > sma_50)  if sma_50  > 0 else None
    out["sma50_gt_200"] = bool(sma_50 > sma_200) if sma_50  > 0 and sma_200 > 0 else None
    out["above_st"]     = bool(close > supertrend) if supertrend > 0 else None
    out["bk_trigger"]   = bool(close > week52_hi * 0.98) if week52_hi > 0 else None
    if week52_hi > week52_lo > 0:
        out["price_location"] = round((close - week52_lo) / (week52_hi - week52_lo) * 100, 2)

    # ── Level 2 — historical series ───────────────────────────────────────
    closes = [h["close"] for h in sym_history if h.get("close")]
    lows   = [h["low"]   for h in sym_history if h.get("low")]
    highs  = [h["high"]  for h in sym_history if h.get("high")]

    def _ret(n: int) -> float | None:
        # Returns None gracefully when history < n sessions
        # Reconciliation will use sheet value automatically for that field
        return (round((close - closes[n]) / closes[n] * 100, 4)
                if len(closes) > n and closes[n] else None)

    out["ret_1w"]        = _ret(5)
    out["ret_1m"]        = _ret(20)
    out["ret_3m"]        = _ret(60)
    out["ret_6m"]        = _ret(120)
    out["ret_12m"]       = _ret(240)
    out["price_6m_ago"]  = round(closes[120], 2) if len(closes) > 120 else None  # v1 BUG FIX
    out["price_12m_ago"] = round(closes[240], 2) if len(closes) > 240 else None  # v1 BUG FIX
    out["close_30d"]     = round(closes[30],  2) if len(closes) > 30  else None  # v1 BUG FIX

    # low_30d — v1 BUG FIX: was computed but never written
    if lows:
        out["low_30d"] = round(min(lows[:30] if len(lows) >= 30 else lows), 2)

    high_30d = float(raw.get("high_30d") or 0)
    low_30d  = out.get("low_30d")
    if high_30d > 0 and low_30d and low_30d > 0:
        out["consol_range"] = round((high_30d - low_30d) / low_30d * 100, 4)

    vol_r  = out.get("vol_ratio") or 0
    consol = out.get("consol_range")
    out["breakout_setup"] = bool(
        close > sma_50 and vol_r > 1.5 and consol is not None and consol < 8
    ) if sma_50 > 0 else None

    # wk_hi_high / wk_hi_low — v1 BUG FIX: was missing
    if len(highs) >= 10 and len(lows) >= 10:
        out["wk_hi_high"] = bool(max(highs[:5]) > max(highs[5:10]))
        out["wk_hi_low"]  = bool(min(lows[:5])  > min(lows[5:10]))

    # ── Level 3 — market-relative (computed later after nifty_ret is known) ──
    # rs_vs_nifty assigned in main() after this function returns

    # ── Previously SHEET_ONLY — now from Supabase lookups ─────────────────
    ev = events.get(sym, {})
    out["upcoming_events"]     = ev.get("upcoming_events")      # nifty_upcoming_events.details
    out["upcoming_event_type"] = ev.get("upcoming_event_type")  # nifty_upcoming_events.purpose
    out["in_master_shortlist"] = bool(sym in msl_set)           # master_shortlist presence
    out["index_membership"]    = index_map.get(sym, "")         # nifty_total_market booleans

    # ── NOT_COMPUTABLE — explicit NULL, not sourced from Sheet ────────────
    out["fii_sector_flow"] = None   # requires paid sector-level FII data

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

    return out


# ── Reconciliation ─────────────────────────────────────────────────────────────

def _numeric_diverged(c_val, s_val, field: str) -> tuple[bool, float]:
    """Returns (is_diverged, delta_pct)."""
    try:
        c_f, s_f = float(c_val), float(s_val)
        threshold = WIDE_THRESHOLD if field in WIDE_TOLERANCE_FIELDS else DIVERGE_THRESHOLD
        delta = abs(c_f - s_f) / abs(s_f) if s_f != 0 else (0.0 if c_f == 0 else 1.0)
        return delta > threshold, round(delta * 100, 2)
    except (TypeError, ValueError):
        return False, 0.0


def reconcile_row(computed: dict, sheet_row: dict,
                  reconcile_enabled: bool, field_trust: dict) -> tuple[dict, dict]:
    """
    Merge computed with sheet_row using field-level trust levels.

    Trust levels:
      RECONCILE     — compare vs sheet. Use computed if within tolerance,
                      sheet if diverged (calibration phase behaviour).
      COMPUTE_ALWAYS— always use computed regardless of sheet value.
      SHEET_ALWAYS  — always use sheet regardless of computed value.

    When reconcile_enabled=False: all fields treated as COMPUTE_ALWAYS.
    """
    if not reconcile_enabled:
        return computed, {"mode": "compute_only"}

    # Start with sheet row as base — preserves fields this script doesn't touch
    final = dict(sheet_row)
    meta  = {
        "mode":            "hybrid",
        "compute_always":  [],
        "computed_match":  [],
        "computed_only":   [],
        "sheet_always":    [],
        "diverged":        {},   # field: {computed, sheet, delta_pct, trust}
    }

    # Fields this script computes (exclude internal helpers)
    computable = {
        k: v for k, v in computed.items()
        if k not in OTHER_SCRIPT_FIELDS
    }

    for field, c_val in computable.items():
        trust = field_trust.get(field, "RECONCILE")
        s_val = sheet_row.get(field)

        # COMPUTE_ALWAYS — verified field, ignore sheet
        if trust == "COMPUTE_ALWAYS":
            final[field] = c_val
            meta["compute_always"].append(field)
            continue

        # SHEET_ALWAYS — confirmed different formula/basis, always keep sheet
        if trust == "SHEET_ALWAYS":
            if s_val is not None:
                final[field] = s_val
                meta["sheet_always"].append(field)
            # If sheet is also NULL, fall through to computed
            else:
                final[field] = c_val
                meta["computed_only"].append(field)
            continue

        # RECONCILE (default) ─────────────────────────────────────────────

        # Sheet has no value → always use computed regardless of trust
        if s_val is None:
            final[field] = c_val
            if c_val is not None:
                meta["computed_only"].append(field)
            continue

        # Both NULL → nothing to do
        if c_val is None and s_val is None:
            continue

        # Compute NULL but sheet has value → keep sheet
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
                # Keep sheet, log divergence for investigation
                final[field] = s_val
                meta["diverged"][field] = {
                    "computed": c_b, "sheet": s_b,
                    "delta_pct": 100.0, "type": "bool_mismatch",
                    "trust": trust,
                    "action": "investigate — field formula may differ",
                }
            continue

        # Numeric comparison
        try:
            diverged, delta_pct = _numeric_diverged(c_val, s_val, field)
            if not diverged:
                final[field] = c_val   # computed matches → use computed
                meta["computed_match"].append(field)
            else:
                # Diverged → use sheet (safe), log for investigation
                final[field] = s_val
                meta["diverged"][field] = {
                    "computed": round(float(c_val), 4),
                    "sheet":    round(float(s_val), 4),
                    "delta_pct": delta_pct,
                    "trust": trust,
                    "action": (
                        f"investigate compute formula — delta {delta_pct:.1f}% "
                        f"({'wide' if field in WIDE_TOLERANCE_FIELDS else 'normal'} tolerance)"
                    ),
                }
            continue
        except (TypeError, ValueError):
            pass

        # String or mixed — use computed if non-null
        final[field] = c_val if c_val is not None else s_val
        if c_val is not None:
            meta["computed_match"].append(field)

    meta["summary"] = {
        "compute_always":  len(meta["compute_always"]),
        "computed_match":  len(meta["computed_match"]),
        "computed_only":   len(meta["computed_only"]),
        "sheet_always":    len(meta["sheet_always"]),
        "diverged":        len(meta["diverged"]),
    }
    return final, meta


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — compute_indicators skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())

    reconcile_enabled = cfg_bool("compute_indicators_reconcile", True)
    mode_label = "HYBRID" if reconcile_enabled else "COMPUTE_ONLY"
    logger.info(
        f"compute_indicators v2.1 — {today} — {mode_label}"
        + (" [DRY RUN]" if DRY_RUN else "")
    )

    # ── 6 bulk queries — no per-symbol calls ──────────────────────────────────
    logger.info("Pass 1: bulk data fetch...")
    raw_rows     = fetch_all_raw(sb, today)
    if not raw_rows:
        logger.warning(f"No chartink_raw_data for {today}")
        return {"status": "no_data"}

    sheet_map    = fetch_sheet_values(sb, today) if reconcile_enabled else {}
    bulk_history = fetch_bulk_history(sb, today)
    events_map   = fetch_upcoming_events(sb, today)
    msl_set      = fetch_msl_symbols(sb, today)
    index_map    = fetch_index_membership(sb)
    nifty_ret    = fetch_nifty_return(sb, today)
    field_trust  = fetch_field_trust(sb) if reconcile_enabled else {}

    logger.info(f"  Nifty 1M return: {nifty_ret}%")
    if reconcile_enabled and field_trust:
        overridden = {f: t for f, t in field_trust.items() if t != "RECONCILE"}
        if overridden:
            logger.info(f"  Field trust overrides: {overridden}")

    # ── Compute + reconcile per symbol ────────────────────────────────────────
    logger.info("Pass 2: computing and reconciling...")
    upsert_rows  = []
    skipped      = 0
    total_always = total_match = total_only = total_sheet_always = 0
    all_diverged: dict = defaultdict(list)

    for raw in raw_rows:
        sym = raw.get("symbol")
        if not sym:
            skipped += 1
            continue
        try:
            computed = compute_from_raw(
                raw, bulk_history.get(sym, []),
                events_map, msl_set, index_map
            )
            # Level 3 — needs nifty_ret from separate query
            if nifty_ret is not None and computed.get("ret_1m") is not None:
                computed["rs_vs_nifty"] = round(computed["ret_1m"] - nifty_ret, 4)
            computed["date"]   = today
            computed["symbol"] = sym

            final, meta = reconcile_row(
                computed, sheet_map.get(sym, {}),
                reconcile_enabled, field_trust
            )
            final["compute_meta"] = json.dumps({
                "computed_at": datetime.now(IST).isoformat(),
                **meta,
            })[:4000]

            upsert_rows.append(final)

            s = meta.get("summary", {})
            total_always      += s.get("compute_always", 0)
            total_match       += s.get("computed_match", 0)
            total_only        += s.get("computed_only", 0)
            total_sheet_always += s.get("sheet_always", 0)
            for field, div in meta.get("diverged", {}).items():
                if isinstance(div, dict) and "delta_pct" in div:
                    all_diverged[field].append(div["delta_pct"])

        except Exception as e:
            logger.warning(f"{sym}: failed — {e}")
            skipped += 1

    # ── Reconciliation summary ────────────────────────────────────────────────
    n = len(upsert_rows)
    logger.info("=" * 65)
    logger.info(f"COMPUTE INDICATORS SUMMARY — {today} — {mode_label}")
    logger.info(f"  Symbols: {n} processed | {skipped} skipped")
    if reconcile_enabled:
        logger.info(f"  COMPUTE_ALWAYS (verified fields):   {total_always:>6} decisions")
        logger.info(f"  COMPUTED_MATCH (within tolerance):  {total_match:>6} decisions")
        logger.info(f"  COMPUTED_ONLY  (sheet was NULL):    {total_only:>6} decisions")
        logger.info(f"  SHEET_ALWAYS   (confirmed diff basis):{total_sheet_always:>5} decisions")
        if all_diverged:
            logger.info(f"  DIVERGED (using sheet, needs review): {len(all_diverged)} field types")
            logger.info(f"  {'Field':<30} {'Avg delta':>10}  {'Symbols':>8}  Action")
            logger.info(f"  {'-'*65}")
            for field, deltas in sorted(all_diverged.items(),
                                        key=lambda x: -sum(x[1]) / len(x[1])):
                avg = sum(deltas) / len(deltas)
                logger.info(
                    f"  {field:<30} {avg:>9.1f}%  {len(deltas):>8}  "
                    f"{'→ set SHEET_ALWAYS' if avg > 15 else '→ investigate formula'}"
                )
            logger.info(f"")
            logger.info(f"  To graduate a verified field to COMPUTE_ALWAYS:")
            logger.info(f"  UPDATE system_config SET value='COMPUTE_ALWAYS'")
            logger.info(f"    WHERE key='compute_trust_<field_name>';")
        else:
            logger.info(f"  No diverged fields — all computed values match sheet ✅")
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

    # ── Batch upsert — 50 rows per call ──────────────────────────────────────
    logger.info(f"Pass 3: writing {n} rows to stock_data_daily...")
    written = 0
    for i in range(0, len(upsert_rows), 50):
        sb.table("stock_data_daily").upsert(
            upsert_rows[i:i+50], on_conflict="date,symbol"
        ).execute()
        written += len(upsert_rows[i:i+50])

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
    parser = argparse.ArgumentParser(description="TradeOS v6 — Compute Indicators v2.1")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="Skip sheet reconciliation — use computed everywhere")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    if args.no_reconcile:
        os.environ["COMPUTE_INDICATORS_RECONCILE"] = "false"
    print(main())
