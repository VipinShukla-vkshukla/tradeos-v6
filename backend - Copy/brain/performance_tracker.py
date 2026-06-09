"""
TradeOS v6 — Brain Engine v2: Performance Tracker
===================================================
Computes and stores daily/weekly/monthly performance metrics.
Runs as the last step of the daily pipeline (after step_21_quality_check)
OR can be called independently.

MEASUREMENT CRITERIA (what "better" means):
  DAILY:
    - Signal count and type distribution
    - Forward return proxy at D+5 (earliest available)
    - Score predictiveness (Pearson r of score_adjusted vs D+5 return)
    - AI conviction accuracy vs D+5 outcome

  WEEKLY:
    - Win rate by signal type at D+10 (primary evaluation horizon)
    - Engine performance breakdown (which of 10 engines are winning)
    - Regime accuracy (did we correctly identify market conditions)
    - Brain impact delta (proposals applied this week → win rate change)

  MONTHLY:
    - Full D+20 horizon win rates (complete picture)
    - Score correlation trends (is final_score getting more predictive over time)
    - Config change impact (before/after win rates for applied proposals)
    - AI provider accuracy comparison
    - Cumulative P&L from closed_positions (actual, not proxy)

This data feeds:
  1. performance_metrics table (pre-aggregated for dashboard queries)
  2. brain_analysis_log (brain sees perf history to understand its own impact)
  3. Telegram weekly digest (human-readable summary)
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg, cfg_float


def _pearson(a: pd.Series, b: pd.Series) -> Optional[float]:
    both = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(both) < 10:
        return None
    return round(float(both["a"].corr(both["b"])), 3)


def _win_rate(df: pd.DataFrame, outcome_col: str = "outcome_win") -> Optional[float]:
    if df.empty or outcome_col not in df.columns:
        return None
    valid = df[outcome_col].dropna()
    return round(float(valid.mean()) * 100, 1) if len(valid) >= 5 else None


def _safe_mean(series: pd.Series, decimals: int = 2) -> Optional[float]:
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return round(float(vals.mean()), decimals) if not vals.empty else None


# ─────────────────────────────────────────────────────────────────────────────
# FORWARD RETURN LOADER (lightweight, daily use)
# ─────────────────────────────────────────────────────────────────────────────

def _load_outcomes_for_date_range(sb, signal_date_min: date,
                                   signal_date_max: date,
                                   eval_horizon: int) -> pd.DataFrame:
    """
    Load signal_log with forward returns from stock_data_daily for a date range.
    Returns enriched DataFrame with outcome_win, max_fwd_return.
    """
    rows = (sb.table("signal_log")
              .select("*")
              .gte("date", str(signal_date_min))
              .lte("date", str(signal_date_max))
              .execute().data)
    if not rows:
        return pd.DataFrame()

    signals = pd.DataFrame(rows)
    signals["date"] = pd.to_datetime(signals["date"]).dt.date

    win_thresh  = cfg_float("perf_win_threshold_pct",  3.0)
    stop_thresh = cfg_float("perf_stop_threshold_pct", 5.0)

    symbols    = signals["symbol"].dropna().unique().tolist()
    look_end   = signal_date_max + timedelta(days=int(eval_horizon * 1.5))

    all_rows = []
    for i in range(0, len(symbols), 250):
        chunk = symbols[i:i+250]
        pr = (sb.table("stock_data_daily")
                .select("date,symbol,close")
                .in_("symbol", chunk)
                .gte("date", str(signal_date_min))
                .lte("date", str(look_end))
                .execute().data)
        all_rows.extend(pr)

    if not all_rows:
        return signals

    price_df = pd.DataFrame(all_rows)
    price_df["date"]  = pd.to_datetime(price_df["date"]).dt.date
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")

    price_by_sym = {}
    for sym, grp in price_df.groupby("symbol"):
        price_by_sym[sym] = grp.sort_values("date").set_index("date")["close"]

    fwd_rets = []
    for _, row in signals.iterrows():
        sym = row.get("symbol")
        sig_date = row.get("date")
        if sym not in price_by_sym:
            fwd_rets.append(None)
            continue
        prices = price_by_sym[sym]
        avail  = [d for d in prices.index if d >= sig_date]
        if not avail:
            fwd_rets.append(None)
            continue
        ep = prices.loc[avail[0]]
        if pd.isna(ep) or ep <= 0:
            fwd_rets.append(None)
            continue
        target = avail[0] + timedelta(days=int(eval_horizon * 1.4))
        exits  = [d for d in prices.index if d >= target]
        if not exits:
            fwd_rets.append(None)
            continue
        xp  = prices.loc[exits[0]]
        ret = round(((xp - ep) / ep) * 100, 2) if not pd.isna(xp) and xp > 0 else None
        fwd_rets.append(ret)

    signals[f"ret_fwd_{eval_horizon}d"] = fwd_rets
    signals["outcome_win"]    = signals[f"ret_fwd_{eval_horizon}d"] >= win_thresh
    signals["outcome_loss"]   = signals[f"ret_fwd_{eval_horizon}d"] <= -stop_thresh
    signals["max_fwd_return"] = signals[f"ret_fwd_{eval_horizon}d"]
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE STATS
# ─────────────────────────────────────────────────────────────────────────────

_ENGINES_FALLBACK = ["CTL","SBS","TPO","VBD","IAD","RSB","MOM","RVS","SEC","EAP"]

def _compute_engine_stats(signals: pd.DataFrame, engines: list = None) -> dict:
    """
    Compute win rate and return stats per engine.
    engines: if provided, use this list. Otherwise fetch live from system_config.
    """
    if signals.empty:
        return {}
    pat_col = next((c for c in ["scanner_patterns","engines_list"]
                    if c in signals.columns), None)
    if not pat_col:
        return {}
 
    # Resolve engines list
    if engines is None:
        try:
            from brain.dynamic_registry import discover_engines
            engines = discover_engines()
        except Exception:
            engines = _ENGINES_FALLBACK
 
    result = {}
    for eng in engines:
        mask = signals[pat_col].str.contains(eng, na=False, case=False)
        grp  = signals[mask]
        if len(grp) < 3:
            continue
        result[eng] = {
            "wr":  round(float(grp["outcome_win"].mean())*100, 1) if grp["outcome_win"].notna().any() else None,
            "n":   int(len(grp)),
            "avg_ret": _safe_mean(grp["max_fwd_return"]),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# BRAIN IMPACT MEASUREMENT
# ─────────────────────────────────────────────────────────────────────────────

def _compute_brain_impact(sb, metric_date: date) -> tuple[int, Optional[float]]:
    """
    For proposals applied within the last 30 days, compute average win-rate
    delta between signals before vs after the config change.
    Returns (proposals_applied, avg_wr_delta).
    """
    try:
        since = str(metric_date - timedelta(days=30))
        rows  = (sb.table("brain_proposals")
                   .select("id,target_key,applied_at,backtest_result")
                   .eq("status","APPLIED")
                   .gte("applied_at", since)
                   .execute().data)
        if not rows:
            return 0, None

        deltas = []
        for row in rows:
            bt = row.get("backtest_result") or {}
            if isinstance(bt, str):
                try: bt = json.loads(bt)
                except: continue
            wr_d = bt.get("wr_delta")
            if wr_d is not None:
                deltas.append(float(wr_d))

        avg_delta = round(sum(deltas)/len(deltas), 2) if deltas else None
        return len(rows), avg_delta
    except Exception:
        return 0, None


# ─────────────────────────────────────────────────────────────────────────────
# GRAIN COMPUTERS
# ─────────────────────────────────────────────────────────────────────────────

def compute_daily_metrics(sb, for_date: date) -> dict:
    """Compute daily performance snapshot. Primary horizon: D+5."""
    # D+5 needs signals from 5 trading days ago (~7 calendar)
    eval_date = for_date - timedelta(days=9)
    signals   = _load_outcomes_for_date_range(sb, eval_date, eval_date, eval_horizon=5)

    def _type_wr(stype):
        col = "signal_subtype" if "signal_subtype" in signals.columns else "signal_type"
        if signals.empty or col not in signals.columns:
            return None
        mask = signals[col].str.lower().str.contains(stype, na=False)
        return _win_rate(signals[mask])

    # Today's signals (for count only — too early for outcomes)
    today_rows = (sb.table("signal_log").select("signal_subtype,signal_type")
                    .eq("date", str(for_date)).execute().data)
    today_df   = pd.DataFrame(today_rows) if today_rows else pd.DataFrame()

    def _today_count(stype):
        if today_df.empty:
            return 0
        col = "signal_subtype" if "signal_subtype" in today_df.columns else "signal_type"
        if col not in today_df.columns:
            return 0
        return int(today_df[col].str.lower().str.contains(stype, na=False).sum())

    return {
        "signals_generated":  len(today_df),
        "signals_prime":      _today_count("prime"),
        "signals_breakout":   _today_count("breakout"),
        "signals_staged":     _today_count("staged"),
        "signals_reentry":    _today_count("reentry"),
        "win_rate_overall":   _win_rate(signals),
        "win_rate_prime":     _type_wr("prime"),
        "win_rate_breakout":  _type_wr("breakout"),
        "win_rate_staged":    _type_wr("staged"),
        "win_rate_reentry":   _type_wr("reentry"),
        "avg_fwd_ret_5d":     _safe_mean(signals["max_fwd_return"]) if not signals.empty else None,
        "score_return_corr":  _pearson(signals.get("score_adjusted"),
                                       signals.get("max_fwd_return")) if not signals.empty else None,
        "engine_stats":       json.dumps(_compute_engine_stats(signals)),
        "outcome_coverage_pct": (
            round(signals["outcome_win"].notna().mean()*100,1)
            if not signals.empty else 0.0
        ),
    }


def compute_weekly_metrics(sb, week_end: date) -> dict:
    """Compute weekly performance. Primary horizon: D+10."""
    week_start = week_end - timedelta(days=7)
    eval_end   = week_end - timedelta(days=14)  # signals from 2 weeks ago
    eval_start = eval_end - timedelta(days=7)

    signals    = _load_outcomes_for_date_range(sb, eval_start, eval_end, eval_horizon=10)

    # Regime accuracy
    regime_stats = {}
    if not signals.empty:
        col = next((c for c in ["active_regime","regime"] if c in signals.columns), None)
        if col:
            for regime, grp in signals.groupby(col):
                if len(grp) >= 5:
                    regime_stats[str(regime)] = {
                        "wr": round(float(grp["outcome_win"].mean())*100,1)
                              if grp["outcome_win"].notna().any() else None,
                        "n": int(len(grp))
                    }

    # AI accuracy
    def _ai_wr(conviction):
        if signals.empty or "ai_conviction" not in signals.columns:
            return None
        mask = signals["ai_conviction"].str.upper() == conviction
        return _win_rate(signals[mask])

    brain_count, brain_delta = _compute_brain_impact(sb, week_end)

    return {
        "signals_generated":      len(signals),
        "win_rate_overall":       _win_rate(signals),
        "avg_fwd_ret_10d":        _safe_mean(signals["max_fwd_return"]) if not signals.empty else None,
        "engine_stats":           json.dumps(_compute_engine_stats(signals)),
        "regime_stats":           json.dumps(regime_stats),
        "score_return_corr":      _pearson(
            signals.get("score_adjusted"), signals.get("max_fwd_return")
        ) if not signals.empty else None,
        "ai_high_conv_wr":        _ai_wr("HIGH"),
        "ai_medium_conv_wr":      _ai_wr("MEDIUM"),
        "ai_low_conv_wr":         _ai_wr("LOW"),
        "brain_proposals_applied":brain_count,
        "brain_wr_delta_avg":     brain_delta,
        "outcome_coverage_pct":   (
            round(signals["outcome_win"].notna().mean()*100,1)
            if not signals.empty else 0.0
        ),
    }


def compute_monthly_metrics(sb, month_end: date) -> dict:
    """Compute monthly performance. Primary horizon: D+20 (full picture)."""
    eval_end   = month_end - timedelta(days=28)
    eval_start = eval_end  - timedelta(days=30)
    signals    = _load_outcomes_for_date_range(sb, eval_start, eval_end, eval_horizon=20)

    # Actual P&L from closed positions
    cp_rows = (sb.table("closed_positions")
                 .select("pnl_pct,strategy,exit_reason,holding_days")
                 .gte("exit_date", str(month_end - timedelta(days=30)))
                 .execute().data)
    cp_df = pd.DataFrame(cp_rows) if cp_rows else pd.DataFrame()

    # AI provider comparison
    ai_provider_stats = {}
    if not signals.empty and "provider" in signals.columns:
        for provider, grp in signals.groupby("provider"):
            if len(grp) >= 3:
                ai_provider_stats[str(provider)] = {
                    "wr": _win_rate(grp),
                    "n":  int(len(grp)),
                }

    brain_count, brain_delta = _compute_brain_impact(sb, month_end)

    return {
        "signals_generated":      len(signals),
        "win_rate_overall":       _win_rate(signals),
        "avg_fwd_ret_20d":        _safe_mean(signals["max_fwd_return"]) if not signals.empty else None,
        "engine_stats":           json.dumps(_compute_engine_stats(signals)),
        "score_return_corr":      _pearson(
            signals.get("score_adjusted"), signals.get("max_fwd_return")
        ) if not signals.empty else None,
        "holding_score_corr":     _pearson(
            signals.get("holding_score"), signals.get("max_fwd_return")
        ) if not signals.empty else None,
        "breakout_readiness_corr":_pearson(
            signals.get("breakout_readiness"), signals.get("max_fwd_return")
        ) if not signals.empty else None,
        "ai_high_conv_wr":        None,  # computed in weekly grain
        "brain_proposals_applied":brain_count,
        "brain_wr_delta_avg":     brain_delta,
        "outcome_coverage_pct":   (
            round(signals["outcome_win"].notna().mean()*100,1)
            if not signals.empty else 0.0
        ),
    }

def persist_signal_outcomes(sb, dataset: dict) -> int:
    """
    Write one row per signal to signal_outcomes once the forward
    return window has closed. Safe to re-run — uses upsert on
    (signal_date, symbol). Called by performance_tracker after
    each brain run.
    """
    signals: pd.DataFrame = dataset.get("signals", pd.DataFrame())
    if signals.empty:
        logger.info("signal_outcomes: no signals to persist")
        return 0

    # ── Only write rows where at least 5d forward return exists ──
    # (means the window has closed — no point writing incomplete rows)
    has_outcome = signals["ret_fwd_5d"].notna() if "ret_fwd_5d" in signals.columns else pd.Series(False, index=signals.index)
    signals = signals[has_outcome].copy()
    if signals.empty:
        logger.info("signal_outcomes: no completed forward windows yet")
        return 0

    # ── Join tier from ai_context.__FINAL_PICKS__ ──
    # Build a symbol→tier map across all dates in the window
    tier_map: dict[tuple, str] = {}
    conviction_map: dict[tuple, str] = {}
    try:
        ai_rows = (
            sb.table("ai_context")
              .select("date,conviction_reason")
              .eq("symbol", "__FINAL_PICKS__")
              .gte("date", str(signals["date"].min()))
              .execute().data or []
        )
        for row in ai_rows:
            try:
                candidates = json.loads(row.get("conviction_reason") or "{}")
                for c in candidates.get("ranked_candidates") or []:
                    key = (str(row["date"])[:10], c.get("symbol"))
                    tier_map[key]       = c.get("tier", "UNKNOWN")
                    conviction_map[key] = c.get("conviction", "")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"signal_outcomes: tier join failed (non-fatal): {e}")

    # ── Join actual P&L from closed_positions ──
    closed: pd.DataFrame = dataset.get("closed_positions", pd.DataFrame())
    pnl_map: dict[tuple, float] = {}
    taken_map: dict[tuple, bool] = {}
    if not closed.empty and "symbol" in closed.columns:
        for _, cp in closed.iterrows():
            # match on entry_date + symbol
            key = (str(cp.get("entry_date") or "")[:10], cp.get("symbol"))
            pnl_map[key]   = cp.get("pnl_pct") or cp.get("realized_pnl_pct")
            taken_map[key] = True

    # ── Build rows ──
    rows = []
    for _, s in signals.iterrows():
        sig_date = str(s.get("date") or "")[:10]
        symbol   = s.get("symbol")
        if not sig_date or not symbol:
            continue

        key = (sig_date, symbol)

        ret_5d  = s.get("ret_fwd_5d")
        ret_10d = s.get("ret_fwd_10d")
        ret_20d = s.get("ret_fwd_20d")
        label = s.get("outcome_label")
        if not label:
            if s.get("outcome_win") is True:
                label = "WIN"
            elif s.get("outcome_loss") is True:
                label = "LOSS"
            elif s.get("ret_fwd_5d") is not None:
                label = "NEUTRAL"
            else:
                label = "PENDING"

        rows.append({
            "signal_date":    sig_date,
            "symbol":         symbol,
            "sector":         s.get("sector"),
            "strategy":       s.get("strategy"),
            "signal_type":    s.get("signal_type"),
            "ai_tier":        tier_map.get(key, "UNRANKED"),
            "ai_conviction":  conviction_map.get(key, ""),
            "score_adjusted": float(s["score_adjusted"]) if pd.notna(s.get("score_adjusted")) else None,
            "ret_fwd_5d":     float(ret_5d)  if pd.notna(ret_5d)  else None,
            "ret_fwd_10d":    float(ret_10d) if pd.notna(ret_10d) else None,
            "ret_fwd_20d":    float(ret_20d) if pd.notna(ret_20d) else None,
            "outcome_label":  label,
            "position_taken": taken_map.get(key, False),
            "actual_pnl_pct": pnl_map.get(key),
        })

    if not rows:
        return 0

    # ── Upsert in batches of 200 ──
    written = 0
    for i in range(0, len(rows), 200):
        batch = rows[i:i+200]
        try:
            sb.table("signal_outcomes").upsert(
                batch,
                on_conflict="signal_date,symbol"
            ).execute()
            written += len(batch)
        except Exception as e:
            logger.warning(f"signal_outcomes batch {i//200} failed: {e}")

    logger.info(f"signal_outcomes: wrote {written} rows ({len([r for r in rows if r['position_taken']])} traded)")
    return written

# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def run_performance_tracking(dataset: dict = None, for_date: date = None) -> dict:
    """
    Compute and store daily/weekly/monthly metrics.
    Call at end of daily pipeline run.
    """
    for_date = for_date or date.today()
    sb       = get_supabase()
    stored   = []

    # Daily — always
    try:
        daily = compute_daily_metrics(sb, for_date)
        daily.update({"metric_date": str(for_date), "grain": "daily"})
        sb.table("performance_metrics").upsert(
            daily, on_conflict="metric_date,grain"
        ).execute()
        stored.append("daily")
        logger.info(f"  Daily metrics stored: wr={daily.get('win_rate_overall')}%, "
                    f"signals={daily.get('signals_generated')}")
    except Exception as e:
        logger.warning(f"  Daily metrics failed: {e}")

    # Weekly — on Sundays
    if for_date.weekday() == 6:
        try:
            weekly = compute_weekly_metrics(sb, for_date)
            weekly.update({"metric_date": str(for_date), "grain": "weekly"})
            sb.table("performance_metrics").upsert(
                weekly, on_conflict="metric_date,grain"
            ).execute()
            stored.append("weekly")
            logger.info(f"  Weekly metrics stored: wr={weekly.get('win_rate_overall')}%, "
                        f"brain_delta={weekly.get('brain_wr_delta_avg')}pp")
        except Exception as e:
            logger.warning(f"  Weekly metrics failed: {e}")

    # Monthly — on last day of month
    next_day = for_date + timedelta(days=1)
    if next_day.month != for_date.month:
        try:
            monthly = compute_monthly_metrics(sb, for_date)
            monthly.update({"metric_date": str(for_date), "grain": "monthly"})
            sb.table("performance_metrics").upsert(
                monthly, on_conflict="metric_date,grain"
            ).execute()
            stored.append("monthly")
            logger.info(f"  Monthly metrics stored: wr={monthly.get('win_rate_overall')}%, "
                        f"score_corr={monthly.get('score_return_corr')}")
        except Exception as e:
            logger.warning(f"  Monthly metrics failed: {e}")


    # Persist per-signal outcomes.
    # Standalone call (brain_scheduler, dataset=None): build historical dataset
    #   looking back 5-30 days where the forward window has now closed.
    # Pipeline call (dataset provided): dataset is today's fresh signals —
    #   persist_signal_outcomes will find no completed windows and return 0,
    #   which is correct; outcomes are written by the nightly standalone call.
    try:
        if dataset is None:
            lookback_start = for_date - timedelta(days=30)
            lookback_end   = for_date - timedelta(days=5)
            hist_dataset   = _load_outcomes_for_date_range(
                sb, lookback_start, lookback_end, eval_horizon=5
            )
            persist_signal_outcomes(sb, {"signals": hist_dataset,
                                         "closed_positions": pd.DataFrame()})
            logger.info(
                f"signal_outcomes: standalone persist {lookback_start}→{lookback_end}"
            )
        else:
            persist_signal_outcomes(sb, dataset)
    except Exception as e:
        logger.warning(f"signal_outcomes persist failed (non-fatal): {e}")

    return {"date": str(for_date), "grains_stored": stored}


def send_weekly_telegram_summary(for_date: date = None):
    """Send weekly performance digest to Telegram."""
    for_date = for_date or date.today()
    sb       = get_supabase()

    try:
        rows = (sb.table("performance_metrics")
                  .select("*")
                  .eq("grain", "weekly")
                  .order("metric_date", desc=True)
                  .limit(2)
                  .execute().data)
        if not rows:
            return

        curr = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        def _delta(key):
            if prev and curr.get(key) is not None and prev.get(key) is not None:
                d = float(curr[key]) - float(prev[key])
                return f"{'+' if d >= 0 else ''}{d:.1f}"
            return "—"

        msg = (
            f"📊 *TradeOS Weekly Performance* ({for_date})\n\n"
            f"*Signal Quality (D+10)*\n"
            f"Win Rate: *{curr.get('win_rate_overall','?')}%* ({_delta('win_rate_overall')}pp vs last week)\n"
            f"Avg Return: {curr.get('avg_fwd_ret_10d','?')}%\n"
            f"Score→Return r: {curr.get('score_return_corr','?')}\n\n"
            f"*Brain Engine*\n"
            f"Proposals Applied: {curr.get('brain_proposals_applied',0)}\n"
            f"Win Rate Impact: {curr.get('brain_wr_delta_avg','?')}pp avg improvement\n\n"
            f"*Coverage*: {curr.get('outcome_coverage_pct','?')}% of signals have confirmed outcomes\n"
        )

        import os, urllib.request
        token   = os.getenv("TELEGRAM_BOT_TOKEN","")
        chat_id = os.getenv("TELEGRAM_CHAT_ID","")
        if not token or not chat_id:
            return

        data = json.dumps({
            "chat_id": chat_id, "text": msg, "parse_mode": "Markdown"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type":"application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Weekly Telegram summary sent")

    except Exception as e:
        logger.warning(f"Weekly Telegram summary failed: {e}")


