"""
Proactive "the daemon went quiet" alert.

WHY THIS IS A SEPARATE PROCESS
------------------------------
Notifier.heartbeat() writes a row to intraday_heartbeat every 15 minutes and
logs a heart. That is proof of life only if something is watching the row. A
daemon cannot detect its own death, and a passive dashboard tile is not a
notification: you have to already be looking at the thing that tells you to
look at it.

This check therefore runs OUTSIDE the daemon, from the existing 30-minute
intraday workflow (cron-job.org -> repository_dispatch), and pushes through
the same Notifier every other event alert uses. Two independent clocks: the
daemon writes, the watcher reads. If the writer stops, the reader notices.

DE-DUPLICATION IS CROSS-PROCESS
-------------------------------
Notifier's own dedup memory is per-process, and this watcher is a short-lived
CLI invoked every 30 minutes — that memory resets on every run. The rearm gate
here is consequently backed by the intraday_alerts table: a HEARTBEAT_STALE
row written by any earlier run suppresses a repeat until the rearm window has
elapsed. Without it, a daemon that never comes back would page every 30
minutes for the rest of the session.

SAFE DEFAULTS
-------------
Outside market hours and on NSE holidays nothing is expected to be running,
so nothing is reported. A missing/unreadable heartbeat row is skipped rather
than treated as stale: a schema or connectivity problem should not manufacture
a daemon-down page. Only a row that is provably older than the threshold fires.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_int, get_supabase
from intraday.config import is_market_open, now_ist

KIND = "HEARTBEAT_STALE"
SYMBOL = "INTRADAY_DAEMON"


def _parse_ts(value) -> datetime | None:
    """Parse an ISO timestamp written by Notifier.heartbeat()."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
        return ts if ts.tzinfo else ts.replace(tzinfo=IST)
    except (ValueError, TypeError):
        return None


def heartbeat_age_minutes(sb, now: datetime | None = None) -> float | None:
    """Age of the last heartbeat in minutes, or None when there is no usable row."""
    now = now or now_ist()
    try:
        rows = (sb.table("intraday_heartbeat")
                  .select("ts").eq("id", 1).execute().data or [])
    except Exception as e:
        logger.debug(f"  heartbeat watch: could not read heartbeat — {e}")
        return None
    if not rows:
        return None
    ts = _parse_ts(rows[0].get("ts"))
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 60.0)


def alerted_recently(sb, now: datetime, rearm_minutes: int) -> bool:
    """Has any earlier run already raised this alert within the rearm window?"""
    cutoff = (now - timedelta(minutes=rearm_minutes)).isoformat()
    try:
        rows = (sb.table("intraday_alerts")
                  .select("ts").eq("kind", KIND).gte("ts", cutoff)
                  .execute().data or [])
    except Exception as e:
        logger.debug(f"  heartbeat watch: could not read prior alerts — {e}")
        return False
    return bool(rows)


def _is_holiday(sb, now: datetime) -> bool:
    """Same fail-open contract as intraday.config.is_holiday()."""
    try:
        rows = (sb.table("nse_holidays").select("date")
                  .eq("date", now.date().isoformat()).execute().data or [])
        return bool(rows)
    except Exception:
        return False


def check(sb=None, notifier=None, now: datetime | None = None) -> dict:
    """
    Evaluate once. Returns a small dict so the CLI can log what it decided.

    Never raises for an expected degradation: no DB, no credentials, no
    channel configured — all degrade to "no alert", the same contract the rest
    of the alerting path honours.
    """
    now = now or now_ist()
    sb = sb or get_supabase()
    result = {"status": "ok", "reason": None, "age_minutes": None, "alerted": False}

    if not is_market_open(now):
        result["reason"] = "outside_market_hours"
        return result
    if _is_holiday(sb, now):
        result["reason"] = "nse_holiday"
        return result

    stale_after = cfg_int("intraday_heartbeat_stale_minutes", 20)
    rearm_after = cfg_int("intraday_heartbeat_rearm_minutes", 120)
    age = heartbeat_age_minutes(sb, now)
    result["age_minutes"] = age
    if age is None:
        result["reason"] = "no_heartbeat_row"
        return result
    if age < stale_after:
        result["reason"] = "heartbeat_fresh"
        return result
    if alerted_recently(sb, now, rearm_after):
        result["reason"] = "recently_alerted"
        return result

    from intraday.notifier import Action, Notifier

    headline = (f"No intraday heartbeat for {age:.0f} min "
                f"(expected every 15 min during market hours)")
    detail = ("The intraday daemon may be down or its lease was lost. "
              "Check the systemd timer / process and the latest "
              "intraday_heartbeat row before trusting live alerts.")
    n = notifier or Notifier(sb)
    result["alerted"] = bool(n.send(Action(
        SYMBOL, KIND, headline, detail, urgency="CRITICAL",
        framework="INTRADAY", rearm=False), force=True))
    result["reason"] = "alert_sent" if result["alerted"] else "delivery_failed"
    return result


def main() -> dict:
    try:
        result = check()
    except Exception as e:
        # A failed health check must not fail the whole intraday workflow:
        # position management and candidate alerts still have to run.
        logger.warning(f"  heartbeat watch failed (non-fatal): {e}")
        result = {"status": "error", "error": str(e)}
    logger.info(f"  heartbeat watch: {result}")
    return result


if __name__ == "__main__":
    print(main())
