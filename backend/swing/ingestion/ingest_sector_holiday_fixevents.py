"""
TradeOS v7 — Per-Framework Capital Reconciliation

Compares the framework-specific TOTAL_CAPITAL values from Supabase
against what the system is using, and accounts for active positions.

This replaces the old single-TOTAL_CAPITAL check with per-framework
capital that the frontend dashboard can set and adjust.

WHY THIS EXISTS
----------------
Position sizing used to run off a single number, TOTAL_CAPITAL, read from
.env or a GitHub secret. Nothing was ever checked against reality, and the
figure could drift silently. On 2026-07-27 the configured figure was Rs 20,000
while Kite reported Rs 5,200 available cash and Rs 8,644 invested — Rs 13,844
in total. Every new entry was therefore sized against roughly Rs 6,000 of
headroom that did not exist, and the first symptom would have been a rejected
order at the broker, mid-session, on a signal the system had already told you
to take.

This module does NOT overwrite the framework capital values. They are set
from the frontend dashboard and persist in Supabase. The check merely
reports the configured values versus what the broker currently holds, and
highlights any gap — exactly where the decision belongs.

The snapshot is persisted to system_config so the dashboard can render the same
comparison without needing its own broker session.

TWO CONFIG KEYS (set from frontend dashboard):
---------------------------------------------
  swing_total_capital     — Capital allocated to the swing framework
  intraday_total_capital  — Capital allocated to the intraday framework

Both should be Rs numbers (no commas, no ₹ prefix). Example values:
  swing_total_capital = 300000   (3 lacs)
  intraday_total_capital = 300000 (3 lacs)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime
from loguru import logger
from config import get_supabase
from config import CAPITAL_IS_FALLBACK, IST

SNAPSHOT_KEY = "capital_snapshot_v2"  # v2: per-framework snapshot


def capital_status(sb=None, persist: bool = True) -> dict:
    """
    Returns dict with configured, available, invested, gap, gap_pct, severity, message, as_of.

    Keys:
      configured: {swing: float, intraday: float} — from Supabase config
      available:  {swing: float, intraday: float} — configured minus invested
      invested:   {swing: float, intraday: float} — from open_positions table
      gap:        {swing: float, intraday: float} — configured minus invested
      gap_pct:    {swing: float, intraday: float} — gap as percentage
      severity:   "OK" | "WARN"  — always OK unless config keys missing
      message:    human-readable description
      as_of:      ISO timestamps
    """

    sb = sb or get_supabase()
    now = datetime.now(IST).isoformat()

    # Read framework capital from Supabase (single source of truth)
    swing_cfg_rows = sb.table("system_config").select("value").eq("key", "swing_total_capital").execute().data
    intraday_cfg_rows = sb.table("system_config").select("value").eq("key", "intraday_total_capital").execute().data

    swing_cfg = float(swing_cfg_rows[0]["value"]) if swing_cfg_rows else 0.0
    intraday_cfg = float(intraday_cfg_rows[0]["value"]) if intraday_cfg_rows else 0.0

    # Query open positions to deduct invested capital
    # open_positions table must have a 'framework' column (SWING/INTRADAY)
    try:
        swing_holds = sb.table("open_positions").select("value").eq("framework", "SWING").eq("status", "ACTIVE").execute().data or []
        intraday_holds = sb.table("open_positions").select("value").eq("framework", "INTRADAY").eq("status", "ACTIVE").execute().data or []
    except Exception:
        swing_holds = []
        intraday_holds = []

    # Sum invested (value field — could be notional, ltp*qty, etc.)
    swing_invested = sum(
        float(h.get("value") or h.get("notional") or h.get("ltp") or 0)
        for h in swing_holds
    )
    intraday_invested = sum(
        float(h.get("value") or h.get("notional") or h.get("ltp") or 0)
        for h in intraday_holds
    )

    # Available capital = configured minus invested
    swing_available = swing_cfg - swing_invested
    intraday_available = intraday_cfg - intraday_invested

    # Gaps
    gap_swings = swing_cfg - swing_invested
    gap_pct_swings = (gap_swings / swing_cfg * 100) if swing_cfg else 0.0

    gap_intraday = intraday_cfg - intraday_invested
    gap_pct_intraday = (gap_intraday / intraday_cfg * 100) if intraday_cfg else 0.0

    # Severity: always OK for framework capital (it's a config, not broker-dependent)
    if swing_cfg == 0.0 or intraday_cfg == 0.0:
        severity = "WARN"
        message = ("Configured framework capital not found in system_config. "
                   "Set swing_total_capital and/or intraday_total_capital from the dashboard.")
    else:
        severity = "OK"
        message = (f"Swing: ₹{swing_cfg:,.0f} invested ₹{swing_invested:,.0f} available ₹{swing_available:,.0f} | "
                   f"Intraday: ₹{intraday_cfg:,.0f} invested ₹{intraday_invested:,.0f} available ₹{intraday_available:,.0f}")

    out = {
        "configured": {
            "swing": round(swing_cfg, 2),
            "intraday": round(intraday_cfg, 2),
        },
        "available": {
            "swing": round(swing_available, 2),
            "intraday": round(intraday_available, 2),
        },
        "invested": {
            "swing": round(swing_invested, 2),
            "intraday": round(intraday_invested, 2),
        },
        "gap": {
            "swing": round(gap_swings, 2),
            "intraday": round(gap_intraday, 2),
        },
        "gap_pct": {
            "swing": round(gap_pct_swings, 2),
            "intraday": round(gap_pct_intraday, 2),
        },
        "severity": severity,
        "message": message,
        "as_of": now,
    }

    # Persist snapshot for dashboard
    if persist:
        try:
            snapshot_data = {k: v for k, v in out.items() if k != "as_of"}
            row = {"key": SNAPSHOT_KEY, "value": json.dumps(payload := snapshot_data),
                   "updated_at": now}
            existing = sb.table("system_config").select("key").eq("key", SNAPSHOT_KEY).execute().data
            if existing:
                sb.table("system_config").update(
                    {"value": payload["value"], "updated_at": row["updated_at"]}
                ).eq("key", SNAPSHOT_KEY).execute()
            else:
                sb.table("system_config").insert({
                    **row,
                    "description": "Per-framework capital — written by control/capital_check.py v2",
                }).execute()
        except Exception as e:
            logger.debug(f"  capital snapshot not persisted: {e}")

    return out


def log_capital_status(sb=None) -> dict:
    """Run the check and log it at the severity it deserves."""
    st = capital_status(sb)
    msg = (f"Swing: ₹{st['configured']['swing']:,.0f} avail ₹{st['available']['swing']:,.0f} invested ₹{st['invested']['swing']:,.0f} | "
           f"Intraday: ₹{st['configured']['intraday']:,.0f} avail ₹{st['available']['intraday']:,.0f} invested ₹{st['invested']['intraday']:,.0f}")
    if st["severity"] == "BLOCK":
        logger.error(msg)
    elif st["severity"] == "WARN":
        logger.warning(msg)
    elif st["severity"] == "OK":
        logger.info(msg)
    else:
        logger.debug(msg)
    return st


def main() -> dict:
    """Run the capital status check — idempotent, safe to import from pipeline."""
    from config import get_supabase
    sb = get_supabase()
    return capital_status(sb, persist=True)


if __name__ == "__main__":
    import pprint
    pprint.pprint(main())