"""
The whole morning, in one command.

    python start_day.py            full sequence
    python start_day.py --check    verify readiness and stop, change nothing

WHAT IT DOES, IN THE ORDER THAT MATTERS
---------------------------------------
    1. preflight     credentials, database, kill switch, trading mode
    2. Kite session  reuse today's token, or open the login and wait for the
                     dashboard callback to store it
    3. dashboard     start the console if it is not already up
    4. intraday      start the market-hours daemon when the market is open
    5. summary       what is live, what is paper, what needs you

WHY THE ORDER IS FIXED
----------------------
Each step is a precondition for the next. Starting the intraday daemon before
the Kite session exists produces a monitor with no prices that reports success —
the exact failure this project has hit repeatedly. So a failed step stops the
sequence rather than letting later steps run on a broken foundation.

The one thing that cannot be automated is the Zerodha login itself: the token
expires daily at 07:30 IST by design, and only you can authenticate. Everything
around it is automated, so the manual part is one browser click.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from config import IST, DRY_RUN, get_supabase, is_kill_switch_active

BASE = Path(__file__).parent
FRONTEND = BASE.parent / "frontend"
DASHBOARD_URL = "http://localhost:3000"


def _ok(msg: str) -> None:
    logger.success(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    logger.error(f"  ✗ {msg}")


def step_preflight() -> bool:
    logger.info("─" * 66)
    logger.info("1 · PREFLIGHT")
    ok = True

    try:
        sb = get_supabase()
        sb.table("system_config").select("key").limit(1).execute()
        _ok("database reachable")
    except Exception as e:
        _fail(f"database unreachable — {e}")
        return False

    if is_kill_switch_active():
        _fail("master kill switch is ACTIVE — nothing will trade until it is cleared")
        ok = False
    else:
        _ok("kill switch clear")

    from credentials_resolver import status
    missing = [r["name"] for r in status()
               if not r["configured"] and r["name"] in (
                   "KITE_API_KEY", "KITE_API_SECRET", "SUPABASE_URL",
                   "SUPABASE_SERVICE_KEY", "DEEPSEEK_API_KEY")]
    if missing:
        _fail(f"missing credentials: {', '.join(missing)}")
        ok = False
    else:
        _ok("credentials present")

    from execution.gates import trading_mode, orders_enabled
    sw, intr = trading_mode("SWING"), trading_mode("INTRADAY")
    logger.info(f"    swing={sw} · intraday={intr} · "
                f"orders={'ON' if orders_enabled() else 'off'} · DRY_RUN={DRY_RUN}")
    if orders_enabled() and sw == "LIVE":
        logger.warning("    SWING IS LIVE — exits and partial books will place real orders")
    return ok


def step_kite(wait_seconds: int = 240) -> bool:
    logger.info("─" * 66)
    logger.info("2 · KITE SESSION")
    from kite.token_manager import get_access_token, get_login_url

    if get_access_token():
        _ok("today's session is already valid")
        return True

    url = get_login_url()
    logger.warning("  No valid session. Opening the Zerodha login in your browser.")
    logger.info(f"    {url}")
    logger.info("    Log in; the dashboard callback stores the token automatically.")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    # Poll rather than prompt: the callback writes the token from another
    # process, so there is nothing for this one to read from stdin.
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(5)
        if get_access_token():
            _ok("session established")
            return True
        logger.info(f"    waiting… {int(deadline - time.time())}s left")
    _fail("no session after waiting — the daemon would run with no prices, so stopping")
    return False


def step_dashboard() -> bool:
    logger.info("─" * 66)
    logger.info("3 · DASHBOARD")
    import urllib.request
    try:
        urllib.request.urlopen(DASHBOARD_URL, timeout=3)
        _ok("already running")
        webbrowser.open(DASHBOARD_URL)
        return True
    except Exception:
        pass

    logger.info("  starting the console…")
    try:
        # DETACHED so the dashboard outlives this script — you want it up all
        # day, not only while the launcher is running.
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
        subprocess.Popen(["npm", "run", "dev"], cwd=str(FRONTEND),
                         shell=(sys.platform == "win32"), creationflags=flags)
    except Exception as e:
        _fail(f"could not start the dashboard — {e}")
        return False

    for _ in range(20):
        time.sleep(2)
        try:
            urllib.request.urlopen(DASHBOARD_URL, timeout=2)
            _ok(f"console up at {DASHBOARD_URL}")
            webbrowser.open(DASHBOARD_URL)
            return True
        except Exception:
            continue
    logger.warning("  dashboard did not answer in time — it may still be compiling")
    return True


def step_intraday() -> bool:
    logger.info("─" * 66)
    logger.info("4 · INTRADAY MONITOR")
    from intraday.config import is_trading_session, is_holiday
    from execution.gates import trading_mode

    if is_holiday():
        logger.info("  NSE holiday — monitor not started")
        return True
    if not is_trading_session():
        logger.info("  outside 09:00–15:40 IST — monitor not started (this is correct)")
        return True

    mode = trading_mode("INTRADAY")
    logger.info(f"  starting the daemon in {mode} mode…")
    try:
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
        subprocess.Popen([sys.executable, "-m", "intraday.run"], cwd=str(BASE),
                         creationflags=flags)
        _ok("monitor started in its own window — close it to stop")
        return True
    except Exception as e:
        _fail(f"could not start the monitor — {e}")
        return False


def summary() -> None:
    from execution.gates import trading_mode, orders_enabled, gtt_enabled
    from intraday.config import autonomy_phase
    logger.info("─" * 66)
    logger.info("READY")
    logger.info(f"  phase {autonomy_phase()} · GTT {'on' if gtt_enabled() else 'off'} · "
                f"orders {'ON' if orders_enabled() else 'off'}")
    logger.info(f"  swing={trading_mode('SWING')} · intraday={trading_mode('INTRADAY')}")
    logger.info(f"  dashboard {DASHBOARD_URL}")
    logger.info("")
    logger.info("  Your part today:")
    logger.info("    · review Today's Trade Plans and place any entries yourself")
    logger.info("    · exits and partial books are automatic where mode is LIVE")
    logger.info("    · stop everything:  UPDATE system_config SET value='true' "
                "WHERE key='master_kill_switch';")


def main(check_only: bool = False) -> int:
    logger.info("═" * 66)
    logger.info("TradeOS — start of day")
    logger.info("═" * 66)

    if not step_preflight():
        logger.error("Preflight failed — stopping before anything is started.")
        return 1
    if check_only:
        logger.info("--check: readiness verified, nothing started.")
        return 0
    if not step_kite():
        return 1
    step_dashboard()
    step_intraday()
    summary()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TradeOS one-command start of day")
    ap.add_argument("--check", action="store_true",
                    help="verify readiness and stop, changing nothing")
    sys.exit(main(ap.parse_args().check))
