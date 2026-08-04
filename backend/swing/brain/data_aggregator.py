"""
TradeOS v7 — Brain Engine v2: Data Aggregator
===============================================
DESIGN PRINCIPLES:
  1. SELECT * everywhere — the brain decides what correlates, not us.
  2. Auto-discover all tables registered in brain_script_registry.
  3. Forward return proxy on stock_data_daily for outcome labelling.
  4. Graceful fallback on any table/field absence — brain never crashes
     because a table doesn't exist or a column is null.
  5. Returns full DataFrames. Quant analyzer and LLM see everything.

OUTCOME PROXY:
  Signal on DATE_D for SYMBOL_S:
    WIN  = any of ret_fwd_5d, ret_fwd_10d, ret_fwd_15d, ret_fwd_20d
           reaches +perf_win_threshold_pct (default 3%)
    LOSS = any horizon hits -perf_stop_threshold_pct (default 5%)
    NEUTRAL = neither

  When closed_positions is populated, actual P&L overrides the proxy.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import get_supabase, cfg, cfg_float


# ─────────────────────────────────────────────────────────────────────────────
# CORE TABLES — always loaded regardless of script registry
# ─────────────────────────────────────────────────────────────────────────────
CORE_TABLES = [
    "signal_log",
    "msl_history",
    "stock_data_daily",
    "ai_context",
    "system_config",
    "lessons",
    "open_positions",
    "closed_positions",
    "brain_proposals",
    "config_change_log",
    "performance_metrics",
    "signal_daily_summary",
]


def _safe_load_table(sb, table: str, filters: list = None,
                     order_col: str = None, limit: int = None) -> pd.DataFrame:
    """
    Load a full table with SELECT *. Returns empty DataFrame on any error.
    Converts date columns, numerics, and JSONB fields automatically.
    """
    try:
        q = sb.table(table).select("*")
        if filters:
            for col, op, val in filters:
                if op == "gte":   q = q.gte(col, val)
                elif op == "lte": q = q.lte(col, val)
                elif op == "eq":  q = q.eq(col, val)
                elif op == "in":  q = q.in_(col, val)
        if order_col:
            q = q.order(order_col, desc=True)
        if limit:
            q = q.limit(limit)
        rows = q.execute().data
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # Auto-convert date columns
        for col in df.columns:
            if col in ("date","snapshot_date","entry_date","exit_date",
                       "metric_date","run_date","created_at","updated_at",
                       "applied_at","changed_at"):
                try:
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    if col in ("date","snapshot_date","entry_date","exit_date","metric_date"):
                        df[col] = df[col].dt.date
                except Exception:
                    pass
        # Auto-convert obvious numeric columns (nulls stay NaN)
        for col in df.columns:
            if df[col].dtype == object:
                try:
                    converted = pd.to_numeric(df[col], errors="coerce")
                    if converted.notna().sum() > df[col].notna().sum() * 0.3:
                        df[col] = converted
                except Exception:
                    pass
        logger.debug(f"  Loaded {table}: {len(df)} rows × {len(df.columns)} cols")
        return df
    except Exception as e:
        logger.warning(f"  Could not load {table}: {e}")
        return pd.DataFrame()


def _load_with_date_window(sb, table: str, date_col: str, days: int,
                           extra_filters: list = None) -> pd.DataFrame:
    since = str(date.today() - timedelta(days=days))
    filters = [(date_col, "gte", since)] + (extra_filters or [])
    return _safe_load_table(sb, table, filters=filters, order_col=date_col)


# ─────────────────────────────────────────────────────────────────────────────
# FORWARD RETURN ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _compute_forward_returns(sb, signals: pd.DataFrame,
                              horizons: list, win_thresh: float,
                              stop_thresh: float) -> pd.DataFrame:
    """
    Enrich signals with forward price returns from stock_data_daily.
    Uses closed_positions actual P&L where available (overrides proxy).
    """
    if signals.empty or "date" not in signals.columns:
        return signals

    # Only evaluate signals old enough to have complete forward windows
    max_horizon = max(horizons)
    eval_cutoff = date.today() - timedelta(days=int(max_horizon * 1.5))
    evaluable_mask = signals["date"].apply(
        lambda d: d <= eval_cutoff if hasattr(d, '__le__') else False
    )
    evaluable = signals[evaluable_mask].copy()

    if evaluable.empty:
        logger.info("  No signals old enough for forward return evaluation")
        return signals

    symbols   = evaluable["symbol"].dropna().unique().tolist()
    min_date  = evaluable["date"].min()
    max_date  = evaluable["date"].max() + timedelta(days=int(max_horizon * 1.5))

    logger.info(f"  Loading forward prices for {len(symbols)} symbols...")

    # Batch load in chunks of 250 (Supabase IN limit)
    all_price_rows = []
    for i in range(0, len(symbols), 250):
        chunk = symbols[i:i+250]
        rows = (sb.table("stock_data_daily")
                  .select("date,symbol,close,high,low")
                  .in_("symbol", chunk)
                  .gte("date", str(min_date))
                  .lte("date", str(max_date))
                  .execute().data)
        all_price_rows.extend(rows)

    if not all_price_rows:
        logger.warning("  No price data available for forward returns")
        return signals

    price_df = pd.DataFrame(all_price_rows)
    price_df["date"]  = pd.to_datetime(price_df["date"]).dt.date
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")
    price_df["high"]  = pd.to_numeric(price_df["high"],  errors="coerce")
    price_df["low"]   = pd.to_numeric(price_df["low"],   errors="coerce")

    # Build per-symbol sorted date index for O(log n) lookups
    price_by_sym = {}
    for sym, grp in price_df.groupby("symbol"):
        grp = grp.sort_values("date").set_index("date")
        price_by_sym[sym] = grp

    def _fwd_return(sym, sig_date, horizon_days):
        if sym not in price_by_sym:
            return None, None
        pdata = price_by_sym[sym]
        dates = pdata.index.tolist()
        entry_dates = [d for d in dates if d >= sig_date]
        if not entry_dates:
            return None, None
        ep = pdata.loc[entry_dates[0], "close"]
        if pd.isna(ep) or ep <= 0:
            return None, None
        target_cal = entry_dates[0] + timedelta(days=int(horizon_days * 1.4))
        exit_dates = [d for d in dates if d >= target_cal]
        if not exit_dates:
            return None, None
        ex_row = pdata.loc[exit_dates[0]]
        xp = ex_row["close"]
        if pd.isna(xp) or xp <= 0:
            return None, None
        ret = round(((xp - ep) / ep) * 100, 3)
        # Also track max adverse (low) and max favorable (high) in window
        window = pdata.loc[entry_dates[0]:exit_dates[0]]
        max_ret = round(((window["high"].max() - ep) / ep) * 100, 3) if not window.empty else ret
        return ret, max_ret

    # Compute all horizons
    ret_cols   = {f"ret_fwd_{h}d": [] for h in horizons}
    max_cols   = {f"max_fwd_{h}d": [] for h in horizons}

    for _, row in evaluable.iterrows():
        for h in horizons:
            ret, maxr = _fwd_return(row["symbol"], row["date"], h)
            ret_cols[f"ret_fwd_{h}d"].append(ret)
            max_cols[f"max_fwd_{h}d"].append(maxr)

    for col, vals in {**ret_cols, **max_cols}.items():
        evaluable[col] = vals

    # Win/loss labels
    ret_col_list = [f"ret_fwd_{h}d" for h in horizons]
    evaluable["max_fwd_return"] = evaluable[ret_col_list].max(axis=1)
    evaluable["min_fwd_return"] = evaluable[ret_col_list].min(axis=1)
    evaluable["outcome_win"]    = evaluable["max_fwd_return"] >= win_thresh
    evaluable["outcome_loss"]   = evaluable["min_fwd_return"] <= -stop_thresh
    evaluable["outcome_label"]  = evaluable.apply(
        lambda r: "WIN" if r.get("outcome_win") else
                 ("LOSS" if r.get("outcome_loss") else "NEUTRAL"), axis=1
    )

    # Override with actual closed_positions where available
    if "closed_positions" in []:  # populated below via dataset merge
        pass  # handled in build_analysis_dataset

    # Merge back
    result = signals.copy()
    new_cols = [c for c in evaluable.columns if c not in signals.columns]
    for col in new_cols:
        result.loc[evaluable.index, col] = evaluable[col]

    n_wins = int(evaluable["outcome_win"].sum())
    n_eval = len(evaluable)
    logger.info(f"  Outcomes: {n_eval} evaluated, {n_wins} wins "
                f"({n_wins/n_eval*100:.1f}%), coverage={n_eval/len(signals)*100:.0f}%")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLOSED POSITIONS OVERRIDE
# ─────────────────────────────────────────────────────────────────────────────

def _override_with_actual_pnl(signals: pd.DataFrame,
                               closed_positions: pd.DataFrame) -> pd.DataFrame:
    """
    Where a signal has a matching closed position, replace the forward-return
    proxy labels with actual realised P&L data. More accurate, always preferred.
    """
    if closed_positions.empty or signals.empty:
        return signals
    if "symbol" not in closed_positions.columns or "pnl_pct" not in closed_positions.columns:
        return signals

    cp = closed_positions[["symbol","entry_date","pnl_pct","exit_reason"]].dropna(subset=["pnl_pct"])
    win_thresh  = cfg_float("perf_win_threshold_pct", 3.0)
    stop_thresh = cfg_float("perf_stop_threshold_pct", 5.0)

    # Build lookup: (symbol, entry_date) → pnl_pct
    cp_lookup = {}
    for _, row in cp.iterrows():
        key = (str(row["symbol"]), str(row.get("entry_date", "")))
        cp_lookup[key] = float(row["pnl_pct"])

    overrides = 0
    for idx, row in signals.iterrows():
        key = (str(row.get("symbol", "")), str(row.get("date", "")))
        if key in cp_lookup:
            actual = cp_lookup[key]
            signals.loc[idx, "max_fwd_return"] = actual
            signals.loc[idx, "outcome_win"]    = actual >= win_thresh
            signals.loc[idx, "outcome_loss"]   = actual <= -stop_thresh
            signals.loc[idx, "outcome_label"]  = (
                "WIN" if actual >= win_thresh else
                ("LOSS" if actual <= -stop_thresh else "NEUTRAL")
            )
            signals.loc[idx, "actual_pnl"]     = actual
            overrides += 1

    if overrides:
        logger.info(f"  Overrode {overrides} proxy labels with actual closed_position P&L")
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# SELF-IMPROVEMENT NOTE MINER
# ─────────────────────────────────────────────────────────────────────────────

def _mine_ai_context(ai_context: pd.DataFrame) -> dict:
    """Extract self-improvement notes and market intel from ai_context."""
    if ai_context.empty:
        return {"improvement_notes": [], "market_intel": []}

    improvement_notes = []
    market_intel      = []

    for _, row in ai_context.iterrows():
        symbol = str(row.get("symbol", ""))
        cr = row.get("conviction_reason", {})
        if isinstance(cr, str):
            try: cr = json.loads(cr)
            except: cr = {}

        if symbol == "__FINAL_PICKS__":
            for note in (cr.get("self_improvement_notes") or []):
                improvement_notes.append({
                    "date":              row.get("date"),
                    "pattern_observed":  note.get("pattern_observed", ""),
                    "suggested_rule":    note.get("suggested_rule", ""),
                    "applies_to_sectors":note.get("applies_to_sectors", []),
                    "confidence":        note.get("confidence", "MEDIUM"),
                    "portfolio_guidance":cr.get("portfolio_guidance"),
                })

        elif symbol == "__MARKET_INTEL__":
            market_intel.append({
                "date":      row.get("date"),
                "tone":      cr.get("market_tone", {}).get("summary", ""),
                "sizing":    cr.get("market_tone", {}).get("position_sizing_guidance", ""),
                "lessons":   cr.get("lessons_generated", []),
                "fii_outlook": cr.get("fii_outlook", {}),
                "near_misses": cr.get("near_miss_upgrades", []),
            })

    return {
        "improvement_notes": improvement_notes,
        "market_intel":      market_intel,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MASTER DATASET BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_analysis_dataset(days: int = 90) -> dict:
    """
    Build the complete analysis dataset. SELECT * everywhere.
    Brain receives all fields — it decides what correlates with what.

    Returns a dict of DataFrames + metadata. Adding a new table = 3 lines here.
    The quant analyzer and LLM synthesizer receive everything.
    """
    sb     = get_supabase()
    since  = str(date.today() - timedelta(days=days))
    today  = str(date.today())

    horizons    = json.loads(cfg("perf_eval_horizons", "[5,10,15,20]"))
    win_thresh  = cfg_float("perf_win_threshold_pct", 3.0)
    stop_thresh = cfg_float("perf_stop_threshold_pct", 5.0)

    logger.info(f"Building analysis dataset ({days}d, win≥{win_thresh}%, stop≤-{stop_thresh}%)...")

    # ── Core signal data ──────────────────────────────────────────────────────
    signals = _load_with_date_window(sb, "signal_log", "date", days)
    logger.info(f"  signal_log: {len(signals)} rows × {len(signals.columns)} cols")

    # ── Enrich with forward returns ────────────────────────────────────────────
    if not signals.empty:
        signals = _compute_forward_returns(sb, signals, horizons, win_thresh, stop_thresh)

    # ── All other tables (full SELECT *) ───────────────────────────────────────
    msl_history       = _load_with_date_window(sb, "msl_history", "snapshot_date", days)
    ai_context        = _load_with_date_window(sb, "ai_context",  "date", days)
    lessons           = _safe_load_table(sb, "lessons")
    open_positions    = _safe_load_table(sb, "open_positions",
                                         filters=[("status","eq","OPEN")])
    closed_positions  = _load_with_date_window(sb, "closed_positions", "exit_date", days)
    perf_history      = _load_with_date_window(sb, "performance_metrics", "metric_date", 180)
    signal_summary    = _load_with_date_window(sb, "signal_daily_summary", "date", days)
    brain_history     = _safe_load_table(sb, "brain_analysis_log",
                                          order_col="run_date", limit=20)
    past_proposals    = _safe_load_table(sb, "brain_proposals",
                                          filters=[("status","eq","APPLIED")],
                                          order_col="applied_at", limit=50)
    config_changes    = _safe_load_table(sb, "config_change_log",
                                          order_col="changed_at", limit=100)
    script_registry   = _safe_load_table(sb, "brain_script_registry")

    # ── Override proxy with actual P&L ────────────────────────────────────────
    if not signals.empty and not closed_positions.empty:
        signals = _override_with_actual_pnl(signals, closed_positions)

    # ── Config as dict ────────────────────────────────────────────────────────
    config_rows = _safe_load_table(sb, "system_config")
    config = {r["key"]: {"value": r["value"], "description": r.get("description", "")}
              for _, r in config_rows.iterrows()} if not config_rows.empty else {}

    # ── AI context parsed ──────────────────────────────────────────────────────
    ai_parsed = _mine_ai_context(ai_context)

    # ── Coverage stats ─────────────────────────────────────────────────────────
    n_signals  = len(signals)
    n_outcomes = int(signals["outcome_label"].notna().sum()) if not signals.empty else 0
    coverage   = round(n_outcomes / n_signals * 100, 1) if n_signals > 0 else 0.0

    # ── Table metadata for brain awareness ────────────────────────────────────
    tables_loaded = {}
    for name, df in [
        ("signal_log", signals), ("msl_history", msl_history),
        ("ai_context", ai_context), ("lessons", lessons),
        ("open_positions", open_positions), ("closed_positions", closed_positions),
        ("performance_metrics", perf_history), ("brain_analysis_log", brain_history),
    ]:
        if not df.empty:
            tables_loaded[name] = {
                "rows": len(df),
                "columns": len(df.columns),
                "fields": list(df.columns),
                "null_pct": {c: round(df[c].isna().mean()*100,1)
                             for c in df.columns if df[c].isna().any()},
            }

    logger.info(f"Dataset ready: {n_signals} signals, {n_outcomes} with outcomes "
                f"({coverage}% coverage), {len(tables_loaded)} tables loaded")

    return {
        # ── DataFrames (full, no column filtering) ──
        "signals":            signals,
        "msl_history":        msl_history,
        "ai_context":         ai_context,
        "lessons":            lessons,
        "open_positions":     open_positions,
        "closed_positions":   closed_positions,
        "perf_history":       perf_history,
        "brain_run_history":  brain_history,
        "past_proposals":     past_proposals,
        "config_changes":     config_changes,
        "script_registry":    script_registry,
        # ── Parsed structures ──
        "config":             config,
        "improvement_notes":  ai_parsed["improvement_notes"],
        "market_intel":       ai_parsed["market_intel"],
        # ── Metadata ──
        "period_days":            days,
        "signals_analyzed":       n_signals,
        "signals_with_outcomes":  n_outcomes,
        "coverage_pct":           coverage,
        "outcomes_available":     n_outcomes >= 15,
        "tables_loaded":          tables_loaded,
        "win_threshold":          win_thresh,
        "stop_threshold":         stop_thresh,
        "eval_horizons":          horizons,
        "signal_summary":         signal_summary,
    }
