"""
TradeOS v7 — Per-Framework Capital Health Check

Checks that the framework-specific capital config values (swing_total_capital,
intraday_total_capital) are set in Supabase. This replaces the old single
TOTAL_CAPITAL drift check with per-framework capital that the frontend dashboard
can set and adjust.

The old check compared a single TOTAL_CAPITAL against the broker account balance.
The new check reports per-framework capital configuration — since these are
operator-chosen values from the dashboard, severity is always OK unless the config
keys are missing entirely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime
from loguru import logger


def check_framework_capital() -> tuple[bool, str]:
    """
    Check per-framework capital configuration from Supabase.

    Returns:
        (is_ok: bool, message: str)
        - is_ok is always True unless config keys are completely missing
        - message reports configured values
    """

    from config import get_supabase
    sb = get_supabase()
    now = datetime.now().astimezone().isoformat()

    # Read framework capital from Supabase (single source of truth)
    swing_cfg_rows = sb.table("system_config").select("value").eq("key", "swing_total_capital").execute().data
    intraday_cfg_rows = sb.table("system_config").select("value").eq("key", "intraday_total_capital").execute().data

    swing_cfg = float(swing_cfg_rows[0]["value"]) if swing_cfg_rows else 0.0
    intraday_cfg = float(intraday_cfg_rows[0]["value"]) if intraday_cfg_rows else 0.0

    # Build message
    parts = []
    if swing_cfg:
        parts.append(f"Swing: ₹{swing_cfg:,.0f}")
    else:
        parts.append("Swing: not configured")

    if intraday_cfg:
        parts.append(f"Intraday: ₹{intraday_cfg:,.0f}")
    else:
        parts.append("Intraday: not configured")

    message = " | ".join(parts)

    # Severity: always OK for framework capital (it's a config the operator chose)
    # The only exception is if neither key is present at all.
    if swing_cfg == 0.0 and intraday_cfg == 0.0:
        return False, "Neither swing_total_capital nor intraday_total_capital found in system_config. " \
                      "Set these from the dashboard."

    return True, message


def log_capital_health() -> None:
    """Run the capital health check and log the result."""
    ok, message = check_framework_capital()
    if ok:
        logger.info(f"Capital config: {message}")
    else:
        logger.error(f"Capital config: {message}")


if __name__ == "__main__":
    ok, message = check_framework_capital()
    print(f"Capital check: {'OK' if ok else 'BLOCK'} — {message}")