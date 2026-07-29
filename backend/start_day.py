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


def step_public_ip() -> bool:
    """
    Has the public IP changed since the last session?

    Zerodha requires a static IP allowlist for ORDER PLACEMENT — read-only calls
    are unaffected, which is why quotes, holdings and GTTs work fine while a
    sell is rejected with "No IPs configured for this app".

    On home broadband the IP changes on every router restart and often on a
    lease renewal, so the allowlist silently goes stale. Discovering that at
    09:46 when a stop needs acting on is the worst possible moment. Checking it
    before the market opens turns a mid-session failure into a two-minute task.

    This cannot be automated further: Kite exposes no API to update the
    allowlist, so a human must paste the new IP into the developer console. The
    only real elimination is a fixed egress IP — a small VPS, or a cloud NAT.
    """
    import urllib.request
    logger.info("─" * 66)
    logger.info("2 · PUBLIC IP (Zerodha order allowlist)")

    ip = None
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                ip = r.read().decode().strip()
                break
        except Exception:
            continue
    if not ip:
        logger.warning("  could not determine the public IP — skipping the check")
        return True

    sb = get_supabase()
    known = ""
    try:
        rows = (sb.table("system_config").select("value")
                  .eq("key", "kite_allowlisted_ip").execute().data or [])
        known = (rows[0]["value"] if rows else "") or ""
    except Exception:
        pass

    if not known:
        logger.warning(f"  current public IP is {ip}, and none is recorded yet.")
        logger.warning("  Add it at https://developers.kite.trade/apps -> your app -> "
                       "allowed IPs,")
        logger.warning("  then record it so future changes are detected:")
        logger.warning(f"    UPDATE system_config SET value='{ip}' "
                       f"WHERE key='kite_allowlisted_ip';")
        return True

    if ip != known:
        _fail(f"public IP has CHANGED: {known} -> {ip}")
        logger.error("  Order placement will be rejected until the allowlist is updated.")
        logger.error("  https://developers.kite.trade/apps -> your app -> allowed IPs")
        logger.error(f"    UPDATE system_config SET value='{ip}' "
                     f"WHERE key='kite_allowlisted_ip';")
        logger.error("  Monitoring and alerts still work — only automated selling is blocked.")
        return True   # not fatal: alerting is more valuable than nothing
    _ok(f"public IP unchanged ({ip})")
    return True


def step_kite(wait_seconds: int = 240) -> bool:
    logger.info("─" * 66)
    logger.info("3 · KITE SESSION")
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
    logger.info("4 · DASHBOARD")
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
    logger.info("5 · LIVE MONITOR  (both books)")
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


def apply_selection(only: str) -> None:
    """
    Make the database agree with what you chose at the prompt.

    Choosing "swing only" cannot just skip starting the local daemon. The
    intraday daemon also runs on the Oracle server, reading the same
    system_config — so skipping it here would leave intraday trading from the
    server while the menu told you it was off. That is the worst kind of switch:
    one that reports a state it does not produce.

    So the selection writes the same rows control_panel.py writes. It turns the
    unselected framework OFF and turns the selected one back ON, because a
    framework left off by yesterday's choice must not stay off silently today.

    Mode (paper vs live) is deliberately NOT touched. That is a separate, rarer
    decision — `tradeos swing live` — and folding it into a daily prompt is how
    it gets made by accident.
    """
    from control_panel import apply, _SWITCHES
    from execution.gates import trading_mode
    from config import get_supabase

    sb = get_supabase()
    want = {"SWING": only in ("both", "swing"),
            "INTRADAY": only in ("both", "intraday")}

    logger.info("─" * 66)
    logger.info(f"0 · SELECTION — {only.upper()}")
    for fw, on in want.items():
        if on:
            # Re-enable in its EXISTING mode, so a framework that was live
            # yesterday does not quietly come back as paper, or vice versa.
            apply(fw, trading_mode(fw).lower(), sb)
        else:
            apply(fw, "off", sb)
            logger.info(f"  {fw} stands down everywhere, including the server "
                        f"daemon — it reads the same rows.")


def main(check_only: bool = False, only: str = "both") -> int:
    logger.info("═" * 66)
    logger.info("TradeOS — start of day")
    logger.info("═" * 66)

    if not step_preflight():
        logger.error("Preflight failed — stopping before anything is started.")
        return 1
    step_public_ip()
    if check_only:
        logger.info("--check: readiness verified, nothing started.")
        return 0

    if only != "both":
        apply_selection(only)

    if not step_kite():
        return 1
    step_dashboard()
    # The daemon starts for EVERY selection, including swing only.
    #
    # Its name is misleading: it is the live price feed and exit monitor for
    # BOTH books. It holds the websocket, and evaluate_positions() routes each
    # position to its own framework's policy. Skipping it for "swing only" would
    # leave swing positions with no real-time stop management at all — the
    # opposite of what choosing swing is asking for.
    #
    # What the selection actually turns off is the intraday ENGINES, and that is
    # done in the database by apply_selection(), which the daemon reads. So the
    # monitor runs, and finds no intraday setups to act on.
    step_intraday()
    summary()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TradeOS one-command start of day")
    ap.add_argument("--check", action="store_true",
                    help="verify readiness and stop, changing nothing")
    ap.add_argument("--only", choices=["both", "swing", "intraday"], default="both",
                    help="which framework to run today (default: both)")
    a = ap.parse_args()
    sys.exit(main(a.check, a.only))
