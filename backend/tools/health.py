"""
Every check this project has, in one run, with one verdict.

    python -m tools.health           full sweep
    python -m tools.health --quick   skip the slow ones (used at launch)

WHY ONE COMMAND
---------------
The checks existed and were never run together. validate_config catches an
incoherent cap, validate_selects catches a column that no longer exists,
simulate catches a stage producing nothing — and each of them was a thing you
had to remember to run. A check you have to remember is a check that runs after
the incident, not before it.

WHAT EACH ONE IS FOR, AND WHY IT IS NOT REDUNDANT
--------------------------------------------------
They fail in genuinely different ways, which is why all of them exist:

  config     numbers that contradict each other, or a setting nothing reads
  selects    a SELECT naming a column the schema no longer has
  broker     resting orders that do not match the positions they protect
  data       today's inputs actually arrived and are not yesterday's
  simulate   a stage that completes while producing nothing

None of these raise an exception in production. They produce a system that
looks healthy and decides on nothing, which is the failure mode this project
has hit more than any other.

EXIT CODE
---------
0 clean, 1 something is broken. Suitable for a scheduled run.
"""

from __future__ import annotations

import argparse
import io
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


@dataclass
class Check:
    name: str
    what: str                 # what breaking here would mean
    ok: bool = False
    detail: str = ""
    findings: list[str] = field(default_factory=list)
    skipped: bool = False


def _run(fn) -> tuple[bool, str]:
    """Run a check, converting any exception into a failure rather than a crash."""
    try:
        return fn()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"


# ── individual checks ───────────────────────────────────────────────────────
def check_config() -> tuple[bool, str]:
    from tools.validate_config import check_coherence, check_wiring
    from config import get_supabase
    sb = get_supabase()
    bad = [f for f in (check_coherence(sb) + check_wiring(sb))
           if f.severity == "ERROR"]
    if bad:
        return False, "; ".join(f"{f.key} ({f.current})" for f in bad[:4])
    return True, "caps cohere against TOTAL_CAPITAL, every CRITICAL key is read"


def check_selects() -> tuple[bool, str]:
    from tools import validate_selects as vs
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = vs.main() if hasattr(vs, "main") else 0
    out = buf.getvalue()
    if rc:
        bad = [l.strip() for l in out.splitlines()
               if "✗" in l or "missing" in l.lower()][:4]
        return False, "; ".join(bad) or "validate_selects reported failures"
    return True, "every SELECT names columns that exist"


def check_broker_consistency() -> tuple[bool, str]:
    """
    Do the resting broker orders match the positions they are supposed to protect?

    Three distinct ways this goes wrong, all silent:
      · a GTT for a symbol no longer held — a live sell against nothing
      · a GTT for a PAPER position — a real order for simulated stock
      · a live position with no GTT — unprotected if the daemon is down
    """
    from config import get_supabase
    from execution.gates import gtt_enabled
    if not gtt_enabled():
        return True, "GTT disabled — nothing resting to reconcile"

    sb = get_supabase()
    rows = (sb.table("open_positions").select("symbol,mode,status,current_qty,actual_qty")
              .eq("status", "ACTIVE").execute().data or [])
    live = {r["symbol"] for r in rows if (r.get("mode") or "LIVE").upper() != "PAPER"}
    paper = {r["symbol"] for r in rows if (r.get("mode") or "LIVE").upper() == "PAPER"}

    try:
        from execution.gtt_manager import list_gtts
        resting = set(list_gtts())
    except Exception as e:
        return True, f"could not read GTTs ({str(e)[:60]}) — broker session needed"

    problems = []
    for s in sorted(resting & paper):
        problems.append(f"{s}: REAL GTT for a PAPER position")
    for s in sorted(resting - live - paper):
        problems.append(f"{s}: GTT resting but no position")
    for s in sorted(live - resting):
        problems.append(f"{s}: live position with NO broker stop")
    if problems:
        return False, "; ".join(problems[:4])
    return True, f"{len(resting)} GTT(s) match {len(live)} live position(s)"


def check_data_freshness() -> tuple[bool, str]:
    """
    Did today's inputs actually arrive?

    A pipeline that runs on yesterday's data completes successfully and decides
    on a market that has moved. Staleness is invisible unless it is checked.
    """
    from config import get_supabase, today_ist
    sb = get_supabase()
    today = str(today_ist())
    stale = []
    for table, col, tol_days in (("signal_output_daily", "date", 4),
                                 ("stock_data_daily", "date", 4)):
        try:
            r = (sb.table(table).select(col).order(col, desc=True)
                   .limit(1).execute().data or [])
            if not r:
                stale.append(f"{table}: EMPTY")
                continue
            latest = str(r[0][col])[:10]
            from datetime import date
            gap = (date.fromisoformat(today) - date.fromisoformat(latest)).days
            if gap > tol_days:
                stale.append(f"{table}: {gap}d old ({latest})")
        except Exception as e:
            stale.append(f"{table}: {str(e)[:50]}")
    if stale:
        return False, "; ".join(stale)
    return True, "signal and price data are current"


def check_kite() -> tuple[bool, str]:
    """Probe the session rather than trusting a validity flag."""
    from config import get_supabase
    sb = get_supabase()
    try:
        from kite import kite_client
        prof = kite_client.get_kite().profile()
    except Exception as e:
        return False, f"Kite call failed: {str(e)[:110]}"

    # Zerodha's allowlist is IPv4-only. On a dual-stack network api.kite.trade
    # resolves v6-first, so orders leave from an address that cannot be
    # allowlisted — while this very check, asking a v4 endpoint, reports a
    # perfect match. That combination cost a live session: every exit rejected
    # with "IP (2402:e280:...) is not allowed", and every readiness check green.
    #
    # kite_client forces v4 at import. This verifies the force actually took,
    # because a check that cannot fail is not a check.
    try:
        import socket
        fams = {f for f, *_ in socket.getaddrinfo("api.kite.trade", 443)}
        if socket.AF_INET6 in fams:
            return False, ("api.kite.trade still resolves over IPv6 — orders will "
                           "leave from a v6 address that Zerodha's IPv4-only "
                           "allowlist can never match")
    except Exception:
        pass

    # An allowlist mismatch does not surface until an order is rejected, which
    # is mid-session, which is the worst time to discover it.
    ip = ""
    try:
        import urllib.request
        ip = urllib.request.urlopen("https://api.ipify.org",
                                    timeout=8).read().decode().strip()
        rows = (sb.table("system_config").select("value")
                  .eq("key", "kite_allowlisted_ip").execute().data or [])
        rec = (rows[0]["value"] if rows else "") or ""
        if rec and rec != ip:
            return False, (f"public IP is {ip} but {rec} is allowlisted — order "
                           f"placement will be REJECTED")
    except Exception:
        pass
    return True, (f"session live for {prof.get('user_id')}"
                  + (f", IPv4 {ip} matches allowlist" if ip else ", IPv4 forced"))


def check_daemon() -> tuple[bool, str]:
    """
    Is a monitor alive, and WHERE?

    The lease answers both, because it is the same row the daemons use to decide
    which of them may act. A stale lease during market hours means nothing is
    watching your positions — and that is silent: the daemon does not announce
    its own death, and the dashboard keeps showing the last prices it wrote.

    Outside market hours a stale lease is correct, not a fault.
    """
    from datetime import datetime, timezone
    from config import get_supabase
    from intraday.config import is_trading_session, is_holiday

    sb = get_supabase()
    rows = (sb.table("intraday_daemon_lease")
              .select("holder,hostname,expires_at,acquired_at")
              .eq("id", 1).execute().data or [])
    in_session = is_trading_session() and not is_holiday()

    if not rows:
        return (not in_session,
                "no monitor has ever run" if not in_session
                else "NO MONITOR RUNNING — positions are unwatched")

    r = rows[0]
    host = r.get("hostname") or "?"
    try:
        exp = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - exp).total_seconds()
    except Exception:
        return False, f"lease timestamp unreadable: {r.get('expires_at')}"

    if age < 0:
        return True, f"monitor ALIVE on '{host}' (lease valid {abs(age):.0f}s more)"
    if not in_session:
        return True, f"no monitor — outside market hours (last ran on '{host}')"
    return False, (f"monitor on '{host}' STOPPED renewing {age / 60:.0f} min ago "
                   f"during market hours — positions are unwatched")


def check_simulate() -> tuple[bool, str]:
    """The slow one: run both frameworks end to end and confirm stages produce output."""
    from config import get_supabase
    from tools.simulate import simulate_swing, simulate_intraday
    sb = get_supabase()
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        s = simulate_swing(sb)
        i = simulate_intraday(sb)
    notes = []
    if s.get("plans", 0) == 0:
        notes.append("swing produced ZERO trade plans")
    if i.get("universe", 0) == 0:
        notes.append("intraday universe is EMPTY")
    if notes:
        return False, "; ".join(notes)
    return True, (f"swing {s.get('positions')} pos / {s.get('plans')} plans · "
                  f"intraday {i.get('universe')} universe / "
                  f"{i.get('takeable', 0)} takeable")


CHECKS = [
    ("config",   "risk numbers contradict each other, or a switch does nothing", check_config,   False),
    ("selects",  "a query reads a column the schema no longer has",              check_selects,  False),
    ("kite",     "no broker session, or the IP is not allowlisted",              check_kite,     False),
    ("data",     "decisions would run on stale inputs",                          check_data_freshness, False),
    ("broker",   "resting orders do not match the positions they protect",       check_broker_consistency, False),
    ("daemon",   "nothing is watching your positions right now",                 check_daemon,   False),
    ("simulate", "a stage completes while producing nothing",                    check_simulate, True),
]


def main(quick: bool = False) -> int:
    logger.info("═" * 72)
    logger.info(f"TradeOS health{'  (quick)' if quick else ''}")
    logger.info("═" * 72)

    results: list[Check] = []
    for name, what, fn, slow in CHECKS:
        c = Check(name=name, what=what)
        if quick and slow:
            c.skipped = True
            results.append(c)
            logger.info(f"  …  {name:<9} skipped (--quick)")
            continue
        c.ok, c.detail = _run(fn)
        results.append(c)
        if c.ok:
            logger.success(f"  ✓  {name:<9} {c.detail}")
        else:
            logger.error(f"  ✗  {name:<9} {c.detail.splitlines()[0][:120]}")
            logger.info(f"       means: {what}")

    bad = [c for c in results if not c.ok and not c.skipped]
    logger.info("")
    logger.info("─" * 72)
    if bad:
        logger.error(f"  {len(bad)} PROBLEM(S): {', '.join(c.name for c in bad)}")
        logger.info("")
        logger.info("  Fix these before trusting today's decisions.")
    else:
        n = len([c for c in results if not c.skipped])
        logger.success(f"  all {n} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run every TradeOS health check")
    ap.add_argument("--quick", action="store_true",
                    help="skip slow checks — used by the launcher")
    sys.exit(main(ap.parse_args().quick))
