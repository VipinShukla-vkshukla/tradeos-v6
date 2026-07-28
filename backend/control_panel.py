"""
Turn a framework on or off, and choose paper or live — from the command line.

WHERE THIS BELONGS, AND WHY IT IS NOT IN start_day.py
------------------------------------------------------
The launcher answers "start today". This answers "what should the system be
doing at all", and those change on completely different rhythms: you launch
every morning, but you promote intraday from paper to live once. Folding a rare,
consequential decision into a daily routine is how it gets made by accident —
the same reasoning that keeps entries manual while exits are automated.

So the daily command stays a daily command, and mode changes are explicit:

    python control_panel.py                    show everything
    python control_panel.py --intraday paper   simulate intraday
    python control_panel.py --intraday live    real money on intraday
    python control_panel.py --intraday off     stop intraday entirely
    python control_panel.py --swing off        stop swing automation

Every change is confirmed against the database afterwards and printed back, so
"I set it" and "it is set" are never assumed to be the same thing — this project
has been bitten by exactly that gap more than once.

The dashboard Control Room writes the same system_config rows. Neither is
authoritative over the other because there is only one place the values live.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from config import DRY_RUN, get_supabase, is_kill_switch_active

# What each framework's on/off actually controls. Two keys for intraday because
# "off" must stop it both finding setups AND acting on them.
_SWITCHES = {
    "INTRADAY": {
        "mode": "intraday_trading_mode",
        "enabled": ["intraday_strategies_enabled", "intraday_auto_exit"],
    },
    "SWING": {
        "mode": "swing_trading_mode",
        "enabled": ["swing_auto_exit"],
    },
}


def _set(sb, key: str, value: str) -> None:
    existing = sb.table("system_config").select("key").eq("key", key).execute().data
    if existing:
        sb.table("system_config").update({"value": value}).eq("key", key).execute()
    else:
        sb.table("system_config").insert(
            {"key": key, "value": value,
             "description": "set from control_panel.py"}).execute()


def apply(framework: str, action: str, sb=None) -> None:
    sb = sb or get_supabase()
    fw = framework.upper()
    sw = _SWITCHES[fw]
    act = action.lower()

    if act == "off":
        for k in sw["enabled"]:
            _set(sb, k, "false")
        logger.warning(f"  {fw} OFF — {', '.join(sw['enabled'])} set false. "
                       f"Open positions are still monitored and alerted; only "
                       f"automated action stops.")
    elif act in ("paper", "live"):
        _set(sb, sw["mode"], act.upper())
        for k in sw["enabled"]:
            _set(sb, k, "true")
        if act == "live":
            logger.warning(f"  {fw} is now LIVE — real orders will be placed.")
        else:
            logger.success(f"  {fw} is now PAPER — decisions run, fills are simulated.")
    else:
        raise SystemExit(f"unknown action {action!r}; use paper, live or off")


def show(sb=None) -> None:
    sb = sb or get_supabase()
    from execution.gates import (trading_mode, orders_enabled, gtt_enabled,
                                 auto_exit_enabled, autonomy_phase)
    from config import cfg_bool

    logger.info("═" * 62)
    logger.info("TradeOS — control panel")
    logger.info("═" * 62)
    logger.info(f"  phase {autonomy_phase()} · orders {'ON' if orders_enabled() else 'off'} "
                f"· GTT {'on' if gtt_enabled() else 'off'} · DRY_RUN={DRY_RUN}")
    if is_kill_switch_active():
        logger.error("  KILL SWITCH ACTIVE — nothing will trade")
    logger.info("")

    for fw in ("SWING", "INTRADAY"):
        mode = trading_mode(fw)
        auto = auto_exit_enabled(fw)
        extra = ""
        if fw == "INTRADAY":
            extra = (" · engines "
                     + ("on" if cfg_bool("intraday_strategies_enabled", True) else "OFF"))
        # LIVE with automation on is the state worth making unmissable.
        tag = "🔴 LIVE" if mode == "LIVE" else "📄 PAPER"
        logger.info(f"  {fw:<9} {tag}  auto-exit {'ON' if auto else 'off'}{extra}")

    logger.info("")
    try:
        rows = (sb.table("open_positions").select("symbol,mode,framework")
                  .eq("status", "ACTIVE").execute().data or [])
        if rows:
            for r in rows:
                logger.info(f"    {r['symbol']:<12} {r.get('mode','?'):<6} "
                            f"{r.get('framework','?')}")
        else:
            logger.info("    no open positions")
    except Exception as e:
        logger.warning(f"    positions unreadable: {e}")

    logger.info("")
    logger.info("  change:  python control_panel.py --intraday paper|live|off")
    logger.info("           python control_panel.py --swing    paper|live|off")


def main() -> int:
    ap = argparse.ArgumentParser(description="Enable, disable, or switch paper/live")
    ap.add_argument("--intraday", choices=["paper", "live", "off"])
    ap.add_argument("--swing", choices=["paper", "live", "off"])
    a = ap.parse_args()

    sb = get_supabase()
    changed = False
    if a.intraday:
        apply("INTRADAY", a.intraday, sb)
        changed = True
    if a.swing:
        apply("SWING", a.swing, sb)
        changed = True

    if changed:
        # Read back rather than trust the write. "I set it" and "it is set" are
        # not the same statement, and this project has been bitten by the gap.
        logger.info("")
        logger.info("  verified from the database:")
    show(sb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
