"""
TradeOS v6 — Append MSL Snapshot to History
Runs daily to preserve ranking trajectory over time
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase, today_ist, DRY_RUN, is_kill_switch_active


def main():
    if is_kill_switch_active():
        return {}
    logger.info("STEP 3: Append MSL History Snapshot")
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
    return {"snapshotted": len(hist_rows), "date": today}


if __name__ == "__main__":
    main()
