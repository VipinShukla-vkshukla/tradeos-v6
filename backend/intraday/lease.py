"""
Exactly one daemon acts at a time — with the other ready to take over.

WHY TWO DAEMONS IS NOT REDUNDANCY
----------------------------------
Running the monitor on a laptop AND a server sounds like insurance. It is not.
Every guard that prevents duplicate action is IN-MEMORY, per process:

    order_manager._recent     stops the same SELL being sent twice
    order_manager._blocked    latches a configuration rejection
    engine._recorded          stops the same setup being recorded repeatedly
    Notifier._last            stops the same alert being sent repeatedly

None of them can see another machine. Two daemons would therefore each place
the same exit order, each open the same paper position, each send every alert
twice, and both push conflicting GTT updates to the broker. Redundancy that
duplicates side effects is worse than no redundancy, because the failure it
creates is silent and expensive while the failure it prevents is loud and
recoverable.

WHAT ACTUAL FAILOVER LOOKS LIKE
-------------------------------
One ACTIVE daemon holds a short lease in Supabase and renews it every cycle.
Any other daemon starts in STANDBY: it watches, it computes, it logs — and it
touches nothing. If the active one dies, its lease expires within
`lease_ttl_seconds` and standby promotes itself automatically.

Supabase is the arbiter because both machines already depend on it completely.
Adding a second coordination service would create a new thing that can fail
independently of the thing it coordinates.

AND THE REAL SAFETY NET IS NOT THIS
-----------------------------------
If both daemons are down, positions still have their broker-side GTT stops
resting at Zerodha. That is the answer to "what if the cloud is not working" —
not a second daemon, but a stop that needs no daemon at all. This module keeps
the two processes from colliding; the GTT keeps you protected when neither is
running.
"""

from __future__ import annotations

import os
import socket
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, get_supabase, cfg_int, cfg

ACTIVE = "ACTIVE"
STANDBY = "STANDBY"

_INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _is_primary() -> bool:
    """
    True on the machine named by intraday_lease_primary_host (migration 050).

    Empty (the default) means no machine is preferred and both sides race
    exactly as migration 023 originally designed — this key existing at all
    must not change behaviour for anyone who has not set it. A hostname
    match is a prefix match against socket.gethostname(), so a configured
    'tradeos-vcn' matches that host with no exact-string fragility.
    """
    p = cfg("intraday_lease_primary_host", "").strip()
    return bool(p) and socket.gethostname().lower().startswith(p.lower())


@dataclass
class LeaseState:
    role: str            # ACTIVE | STANDBY
    holder: str
    since: datetime | None
    detail: str

    @property
    def may_act(self) -> bool:
        """Standby computes everything and commits nothing."""
        return self.role == ACTIVE


def _ttl() -> int:
    # Long enough that an ordinary slow cycle does not drop the lease, short
    # enough that a dead process is replaced inside a couple of minutes. A
    # position needing an exit cannot wait ten minutes for a takeover.
    return cfg_int("intraday_lease_ttl_seconds", 120)


def _now() -> datetime:
    return datetime.now(IST)


def acquire(sb=None) -> LeaseState:
    """
    Claim the lease, or report that someone else holds it.

    Read-then-write rather than a database lock: PostgREST offers no advisory
    locks, and the race window is one cycle. Two daemons starting in the same
    second could briefly both believe they are active — which is why the holder
    id is re-checked on every renew, and the loser stands down as soon as it
    notices.

    A configured primary (see _is_primary) skips the deference check below
    entirely and always claims. Nothing else changes — same table, same TTL,
    same upsert.
    """
    sb = sb or get_supabase()
    now = _now()
    am_primary = _is_primary()
    try:
        rows = (sb.table("intraday_daemon_lease").select("*")
                  .eq("id", 1).execute().data or [])
    except Exception as e:
        # No lease table means no coordination is possible. Acting anyway is the
        # lesser evil for a single-machine setup, but say so — silently assuming
        # exclusivity is how two daemons end up both trading.
        logger.warning(f"  lease table unavailable ({e}) — running WITHOUT "
                       f"coordination. If a second daemon is running anywhere, "
                       f"both will act.")
        return LeaseState(ACTIVE, _INSTANCE_ID, now, "no lease table")

    if rows and not am_primary:
        r = rows[0]
        holder = r.get("holder") or ""
        try:
            expires = datetime.fromisoformat(
                str(r.get("expires_at")).replace("Z", "+00:00")).astimezone(IST)
        except Exception:
            expires = now - timedelta(seconds=1)

        if holder and holder != _INSTANCE_ID and expires > now:
            left = int((expires - now).total_seconds())
            return LeaseState(
                STANDBY, holder, None,
                f"{holder} holds the lease for another {left}s — this process is "
                f"STANDBY and will take over automatically if that stops renewing")

    patch = {
        "id": 1,
        "holder": _INSTANCE_ID,
        "hostname": socket.gethostname(),
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=_ttl())).isoformat(),
    }
    try:
        sb.table("intraday_daemon_lease").upsert(patch, on_conflict="id").execute()
    except Exception as e:
        logger.warning(f"  could not write lease: {e}")
        return LeaseState(ACTIVE, _INSTANCE_ID, now, "lease write failed — acting anyway")

    detail = "lease acquired — this host is the configured primary" if am_primary else "lease acquired"
    return LeaseState(ACTIVE, _INSTANCE_ID, now, detail)


def renew(sb=None) -> LeaseState:
    """
    Extend the lease, and stand down if someone else has taken it.

    The second half matters: a daemon that paused long enough to lose its lease
    must NOT resume acting when it wakes up, because a standby has almost
    certainly promoted itself by then — UNLESS this is the configured primary,
    which reclaims here on the very next renew rather than waiting to be
    restarted. That is the difference between "primary at startup" and
    "primary" — without it, the 2026-08-06 stall (lease lost mid-run to a
    starved loop, not lost at boot) would have left the server demoted until
    someone manually restarted it.
    """
    sb = sb or get_supabase()
    now = _now()
    am_primary = _is_primary()
    try:
        rows = (sb.table("intraday_daemon_lease").select("holder,expires_at")
                  .eq("id", 1).execute().data or [])
        if rows and not am_primary and (rows[0].get("holder") or "") != _INSTANCE_ID:
            return LeaseState(
                STANDBY, rows[0].get("holder") or "?", None,
                "another daemon holds the lease — standing down to avoid "
                "duplicate orders and alerts")
        sb.table("intraday_daemon_lease").upsert({
            "id": 1, "holder": _INSTANCE_ID, "hostname": socket.gethostname(),
            "expires_at": (now + timedelta(seconds=_ttl())).isoformat(),
        }, on_conflict="id").execute()
        detail = "renewed — configured primary" if am_primary else "renewed"
        return LeaseState(ACTIVE, _INSTANCE_ID, now, detail)
    except Exception as e:
        # A transient database error must not hand the book to a standby that
        # may also be struggling. Keep acting; the lease will lapse on its own
        # if the problem persists.
        logger.debug(f"  lease renew failed: {e}")
        return LeaseState(ACTIVE, _INSTANCE_ID, now, f"renew failed: {e}")


def release(sb=None) -> None:
    """Give up the lease on a clean shutdown so failover is immediate."""
    sb = sb or get_supabase()
    try:
        rows = (sb.table("intraday_daemon_lease").select("holder")
                  .eq("id", 1).execute().data or [])
        if rows and (rows[0].get("holder") or "") == _INSTANCE_ID:
            sb.table("intraday_daemon_lease").upsert({
                "id": 1, "holder": "", "hostname": socket.gethostname(),
                "expires_at": _now().isoformat(),
            }, on_conflict="id").execute()
            logger.info("  lease released — a standby can take over immediately")
    except Exception as e:
        logger.debug(f"  lease release failed: {e}")


def instance_id() -> str:
    return _INSTANCE_ID
