"""
TradeOS v6 — Append MSL Snapshot to History
Runs daily as step 06_history in run_pipeline.py (Phase 0/1) or 09_history (Phase 2+).

BUG FIXED (01-Apr-2026 audit):
  regime_history table uses 'date' as primary key column.
  G14 _snapshot_regime() was incorrectly setting row["snapshot_date"] = today
  and upsert on_conflict="snapshot_date" — neither column exists in regime_history.
  Result: every daily snapshot silently failed with a Supabase 400 error,
  leaving regime_history permanently empty despite the G14 patch appearing to run.
  Fix: strip all columns not in regime_history schema, set row["date"] = today,
  upsert on_conflict="date".
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from loguru import logger
from config import get_supabase, today_ist, DRY_RUN, is_kill_switch_active
from config import cfg

# Exact columns in regime_history (from schema dump Mar 23 + live Supabase):
# AFTER:
REGIME_HISTORY_COLS = {
    # Original fields
    "date", "regime", "nifty_price", "nifty_50dma", "nifty_200dma",
    "nifty_weekly_rsi", "india_vix", "avg_sector_breadth",
    "ctl_enabled", "sbs_enabled", "tpo_enabled", "eap_enabled",
    "regime_score", "nifty_1d_chg_pct", "nifty_5d_chg_pct",
    "nifty_20d_chg_pct", "advance_decline_ratio", "above_200dma_pct",
    # Added by compute_regime v2.0 — required for ML classifier training
    "computed_regime",       # rule-based computed regime (vs manual sheet regime)
    "regime_score_computed", # 0–100 score that drove computed_regime
    "vix_5d_delta",          # VIX direction signal (falling vs rising)
    "midcap_breadth",        # smallcap/midcap divergence detector
    "smallcap_breadth",
    "gift_premium_pct",      # GIFT Nifty pre-open signal
    "sp500_5d_ret",          # global cue
    "usdinr_5d_chg",         # rupee direction signal
    "banknifty_weekly_rsi",  # banking sector confirmation
}


def resolve_last_trading_day(sb, reference_date: str, max_lookback: int = 10) -> str:
    """
    Walk backwards from reference_date to find the last valid trading day:
      - Not a Saturday or Sunday
      - Not in nse_holidays table
      - Has actual data in market_regime (populated earlier in the pipeline,
        present in both Phase 0/1 and Phase 2+ runs)

    Raises RuntimeError if no valid day is found within max_lookback days.
    """
    from datetime import datetime, timedelta as _td
    holiday_rows = sb.table("nse_holidays").select("date").execute().data
    holidays     = {row["date"] for row in holiday_rows}
    candidate    = datetime.strptime(reference_date, "%Y-%m-%d").date()

    logger.info(f"  Resolving trading day from {reference_date} (holidays loaded: {len(holidays)})")

    for _ in range(max_lookback):
        date_str = str(candidate)
        dow      = candidate.weekday()   # 0=Mon … 6=Sun

        if dow in (5, 6):
            logger.debug(f"  {date_str} is {'Saturday' if dow == 5 else 'Sunday'} — stepping back")
            candidate -= _td(days=1)
            continue

        if date_str in holidays:
            occasion = next(
                (r.get("occasion", "NSE holiday") for r in holiday_rows if r["date"] == date_str),
                "NSE holiday"
            )
            logger.debug(f"  {date_str} is an NSE holiday ({occasion}) — stepping back")
            candidate -= _td(days=1)
            continue

        probe = (
            sb.table("market_regime")
              .select("date")
              .eq("date", date_str)
              .limit(1)
              .execute().data
        )
        if probe:
            if date_str != reference_date:
                logger.info(f"  Trading day resolved: {reference_date} → {date_str}")
            else:
                logger.info(f"  Trading day confirmed: {date_str}")
            return date_str

        logger.debug(f"  {date_str} — no market_regime rows — stepping back")
        candidate -= _td(days=1)

    raise RuntimeError(
        f"No valid trading day with market_regime data found "
        f"within {max_lookback} days before {reference_date}. "
        f"Ensure compute_regime has run for a recent trading day."
    )


def main():
    if is_kill_switch_active():
        return {}
    logger.info("STEP: Append MSL + Regime History Snapshot")
    sb    = get_supabase()
    today = str(today_ist())

    # Resolve last valid trading day before any queries — today_ist() is wrong on
    # weekends and NSE holidays, causing the duplicate-check to miss an existing
    # snapshot and then fail to find any source rows (or snapshot with a bad date).
    try:
        today = resolve_last_trading_day(sb, today)
    except RuntimeError as exc:
        logger.error(str(exc))
        return {"status": "no_data"}

    existing = sb.table("msl_history").select("symbol").eq("snapshot_date", today).limit(1).execute().data
    if existing:
        logger.info(f"History snapshot already exists for {today} — skipping")
        return {"skipped": True}

    # Read today's MSL.
    #
    # msl_computed was the shadow-mode target for screen_stocks and has not
    # been written since 2026-04-29 (screener_mode moved to 'full', so both
    # screen_stocks and compute_msl write straight to master_shortlist).
    # Retired in migration 008; master_shortlist is now the only source.
    rows = sb.table("master_shortlist").select(
        "date,symbol,company_name,base_rank,priority_rank,sector,strategy_source,"
        "current_price,price_location,dist_fv_pct,entry_timing_type,momentum_phase,"
        "velocity_state,trend_maturity,struct_edge,in_position,reentry_mode,lifecycle,"
        "expected_r,validity_score,final_score,momentum_state,holding_score,risk_score,"
        "breakout_readiness,persistent_phase"
    ).eq("date", today).execute().data

    if not rows:
        logger.warning("No MSL rows for today — skipping history")
        return {"skipped": True}

    hist_rows = []
    for r in rows:
        hist_rows.append({
            "snapshot_date":    today,
            "symbol":           r["symbol"],
            "company_name":     r.get("company_name"),
            "priority_rank":    r.get("base_rank"),
            "sector":           r.get("sector"),
            "strategy_source":  r.get("strategy_source"),
            "close_price":      r.get("current_price"),
            "price_location":   r.get("price_location"),
            "dist_fv_pct":      r.get("dist_fv_pct"),
            "entry_timing_type":r.get("entry_timing_type"),
            "momentum_phase":   r.get("momentum_phase"),
            "velocity_state":   r.get("velocity_state"),
            "trend_maturity":   r.get("trend_maturity"),
            "struct_edge":      r.get("struct_edge"),
            "in_position":      r.get("in_position"),
            "reentry_mode":     r.get("reentry_mode"),
            "lifecycle":        r.get("lifecycle"),
            "expected_r":       r.get("expected_r"),
            "validity_score":   r.get("validity_score"),
            "final_score":      r.get("final_score"),
            "momentum_state":   r.get("momentum_state"),
            "holding_score":    r.get("holding_score"),
            "risk_score":       r.get("risk_score"),
            "breakout_readiness": r.get("breakout_readiness"),
            "persistent_phase":   r.get("persistent_phase"),
        })

    if not DRY_RUN:
        for i in range(0, len(hist_rows), 200):
            sb.table("msl_history").upsert(
                hist_rows[i:i+200], on_conflict="snapshot_date,symbol"
            ).execute()

    logger.info(f"✓ MSL History: {len(hist_rows)} rows snapshotted for {today}")

    # G14: Snapshot today's market_regime into regime_history
    _snapshot_regime(sb, today)

    return {"snapshotted": len(hist_rows), "date": today}


def _snapshot_regime(sb, today: str) -> None:
    """
    G14 (FIXED): Write today's market_regime row into regime_history.
    
    BUG FIXED: was setting row["snapshot_date"] and on_conflict="snapshot_date".
    regime_history has no snapshot_date column — its PK is 'date'.
    Fix: filter to only REGIME_HISTORY_COLS, set row["date"] = today,
         upsert on_conflict="date".
    
    This populates the training source for ml_regime_classifier weekly training (W2).
    Without this fix, regime_history stays empty and classifier falls back to
    market_regime only (still works, but loses the richer daily snapshot data).
    """
    try:
        regime_rows = (
            sb.table("market_regime")
            .select("*")
            .eq("date", today)
            .limit(1)
            .execute()
            .data
        )
        if not regime_rows:
            # Fallback: most recent row
            regime_rows = (
                sb.table("market_regime")
                .select("*")
                .order("date", desc=True)
                .limit(1)
                .execute()
                .data
            )
        if not regime_rows:
            logger.debug("G14: No market_regime rows found — regime_history skipped")
            return

        raw = regime_rows[0]

        # FIX: only include columns that exist in regime_history schema
        row = {k: v for k, v in raw.items() if k in REGIME_HISTORY_COLS}

        # FIX: use 'date' column (regime_history PK), not 'snapshot_date'
        row["date"] = today

        if not DRY_RUN:
            sb.table("regime_history").upsert(
                row, on_conflict="date"          # FIX: was on_conflict="snapshot_date"
            ).execute()

        logger.info(
            f"✓ regime_history snapshot: regime={raw.get('regime','?')} "
            f"nifty_5d={raw.get('nifty_5d_chg_pct','N/A')} "
            f"ad_ratio={raw.get('advance_decline_ratio','N/A')} "
            f"for {today}"
        )
    except Exception as e:
        logger.warning(f"regime_history snapshot failed (non-fatal): {e}")


if __name__ == "__main__":
    main()