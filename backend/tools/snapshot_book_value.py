"""
TradeOS v6 — daily book-value snapshot, for the Swing/Intraday daily
summary dashboard.

Daily Summary Dashboard, swing framework evolution blueprint, 26-Aug-2026.
Writes one row per (date, framework) to book_value_snapshots: sleeve +
all-time realized P&L + live unrealized P&L for that book. Frontend
computes "today's change" as latest live book value minus the most recent
prior snapshot — genuinely accurate from the day this starts running
forward; historical days before it existed cannot be reconstructed, and
the frontend says so rather than guessing.

Run manually first (`python -m tools.snapshot_book_value`); wiring it into
a scheduled run is a deliberate follow-up, not done by this session — no
GitHub Actions workflow is touched here.

READ-ONLY against open_positions/closed_positions for BOTH frameworks
(including INTRADAY) — this is display data, not a trading decision, and
is the one explicitly-granted exception to "no intraday file is touched"
for this specific dashboard feature. Nothing here writes to any
intraday-owned table or changes intraday behaviour.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config import IST, capital_for, get_supabase, today_ist

FRAMEWORKS = ("SWING", "INTRADAY")


def compute_book_value(sb, framework: str) -> dict:
    """
    sleeve + all-time realized P&L + live unrealized P&L for one book.
    Both position tables are read unpaged here, matching this codebase's
    own established convention for these two tables everywhere else
    (open_positions/closed_positions are not in the measured-large-table
    set static analysis enforces paging for).
    """
    sleeve = capital_for(framework)

    closed = (sb.table("closed_positions").select("realized_pnl")
                .eq("framework", framework).execute().data or [])
    realized_cum = sum(float(r.get("realized_pnl") or 0) for r in closed)

    open_rows = (sb.table("open_positions")
                   .select("unrealized_pnl,current_value,invested_value")
                   .eq("framework", framework).eq("status", "ACTIVE")
                   .execute().data or [])
    unrealized = 0.0
    for r in open_rows:
        u = r.get("unrealized_pnl")
        if u is None:
            u = float(r.get("current_value") or 0) - float(r.get("invested_value") or 0)
        unrealized += float(u or 0)

    return {
        "sleeve": round(sleeve, 2),
        "realized_pnl_cum": round(realized_cum, 2),
        "unrealized_pnl": round(unrealized, 2),
        "book_value": round(sleeve + realized_cum + unrealized, 2),
    }


def snapshot(sb=None) -> list[dict]:
    sb = sb or get_supabase()
    trade_date = today_ist().isoformat()
    rows = []
    for fw in FRAMEWORKS:
        try:
            metrics = compute_book_value(sb, fw)
        except Exception as e:
            logger.error(f"  snapshot_book_value: {fw} computation failed — {e}")
            continue
        row = {"date": trade_date, "framework": fw,
              "created_at": datetime.now(IST).isoformat(), **metrics}
        rows.append(row)
        logger.info(f"  {fw}: sleeve ₹{metrics['sleeve']:,.0f} + realized "
                   f"₹{metrics['realized_pnl_cum']:,.0f} + unrealized "
                   f"₹{metrics['unrealized_pnl']:,.0f} = book value "
                   f"₹{metrics['book_value']:,.0f}")

    if rows:
        try:
            sb.table("book_value_snapshots").upsert(
                rows, on_conflict="date,framework").execute()
        except Exception as e:
            logger.error(f"  snapshot_book_value: write failed — {e}")
            return []
    return rows


def main() -> None:
    logger.info("=" * 60)
    logger.info("Book value snapshot")
    logger.info("=" * 60)
    rows = snapshot()
    logger.info(f"  {len(rows)} snapshot(s) written for {today_ist().isoformat()}")


if __name__ == "__main__":
    main()
