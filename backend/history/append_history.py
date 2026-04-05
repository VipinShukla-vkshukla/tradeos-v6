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
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase, today_ist, DRY_RUN, is_kill_switch_active

# Exact columns in regime_history (from schema dump Mar 23 + live Supabase):
REGIME_HISTORY_COLS = {
    "date", "regime", "nifty_price", "nifty_50dma", "nifty_200dma",
    "nifty_weekly_rsi", "india_vix", "avg_sector_breadth",
    "ctl_enabled", "sbs_enabled", "tpo_enabled", "eap_enabled",
    "regime_score", "nifty_1d_chg_pct", "nifty_5d_chg_pct",
    "nifty_20d_chg_pct", "advance_decline_ratio", "above_200dma_pct",
}


def main():
    if is_kill_switch_active():
        return {}
    logger.info("STEP: Append MSL + Regime History Snapshot")
    sb    = get_supabase()
    today = str(today_ist())

    # Check if already snapshotted today
    existing = sb.table("msl_history").select("symbol").eq("snapshot_date", today).limit(1).execute().data
    if existing:
        logger.info(f"History snapshot already exists for {today} — skipping")
        return {"skipped": True}

    # Read today's MSL
    rows = sb.table("master_shortlist").select(
        "date,symbol,company_name,base_rank,sector,strategy_source,current_price,"
        "price_location,dist_fv_pct,entry_timing_type,momentum_phase,velocity_state,"
        "trend_maturity,struct_edge,in_position,reentry_mode,lifecycle,expected_r,"
        "validity_score,final_score"
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