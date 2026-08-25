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
from config import IST, get_supabase, cfg_int, cfg, cfg_bool

ACTIVE = "ACTIVE"
STANDBY = "STANDBY"

_TABLE = "intraday_daemon_lease"

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


@dataclass
class LeaseView:
    """
    A read-only snapshot of the lease row. Plain data, no clock inside — the
    two booleans are resolved against the clock once, by observe(), so anything
    reasoning about them can be tested without one.
    """
    holder: str          # "" when nobody holds it
    hostname: str        # the machine that last held it
    held_by_me: bool     # THIS process is the holder
    held_by_other: bool  # a DIFFERENT process holds it and it has NOT expired
    readable: bool       # the row could be read at all
    detail: str


def observe(sb=None) -> LeaseView:
    """
    Who holds the lease right now, WITHOUT touching it.

    acquire() and renew() both WRITE. Either one, called from a process that
    only wants to know the answer — the pipeline's exit path, a preflight, a
    health check — would take the lease away from the daemon that legitimately
    holds it. That is the precise failure this module exists to prevent, so the
    question "may I act?" needs a form that cannot answer itself by making the
    answer true. This is that form, and it is the only lease function in this
    module that issues no write.

    `readable` distinguishes "nobody holds it" from "I could not find out",
    which are opposite facts: the first means it is free, the second means
    nothing is known. Collapsing them into one empty holder is how a database
    blip would read as permission.
    """
    sb = sb or get_supabase()
    now = _now()
    try:
        rows = (sb.table("intraday_daemon_lease")
                  .select("holder,hostname,expires_at")
                  .eq("id", 1).execute().data or [])
    except Exception as e:
        return LeaseView("", "", False, False, False, f"lease unreadable: {e}")

    if not rows:
        return LeaseView("", "", False, False, True, "no lease row — no daemon has ever run")

    r = rows[0]
    holder = r.get("holder") or ""
    host = r.get("hostname") or "?"
    try:
        expires = datetime.fromisoformat(
            str(r.get("expires_at")).replace("Z", "+00:00")).astimezone(IST)
    except Exception:
        # An unreadable timestamp is an EXPIRED lease, not a live one: the
        # conservative direction here is "nobody is driving", because the
        # opposite reading would let a corrupt row silently forbid every exit.
        expires = now - timedelta(seconds=1)

    mine = bool(holder) and holder == _INSTANCE_ID
    other = bool(holder) and not mine and expires > now
    if mine:
        detail = "this process holds the lease"
    elif other:
        detail = (f"{holder} (on '{host}') holds the lease for another "
                  f"{int((expires - now).total_seconds())}s")
    elif holder:
        detail = (f"{holder} (on '{host}') last held the lease; it expired "
                  f"{int((now - expires).total_seconds())}s ago")
    else:
        detail = f"the lease is free (last held on '{host}')"
    return LeaseView(holder, host, mine, other, True, detail)


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


# ═══════════════════════════════════════════════════════════════════════════
# STARTUP EXCLUSION — migration 077
# ═══════════════════════════════════════════════════════════════════════════
#
# Everything above this line is a ROLE, and a role is not a mutex. On
# 2026-08-10 09:36 two daemons placed six real orders into one live account
# inside 62 seconds, interleaved with nine echoes of the account-wide latch
# that forbids them — a latch with one assignment site and no reset, so one
# process cannot produce that sequence. Three properties of the code above
# allowed it, and all three are visible in this file:
#
#   1. acquire() READS the row (line ~118) and then upserts UNCONDITIONALLY
#      (line ~153). It never re-asserts what it read. Its own docstring says
#      so: "Two daemons starting in the same second could briefly both believe
#      they are active."
#   2. The loser is not told. It finds out at its next renew(), which run.py
#      calls on a 30s timer while the engine evaluates every 15s — so a
#      demoted daemon places orders for one or two more full cycles.
#   3. _is_primary() lets a configured primary skip the deference check and
#      claim a LIVE, unexpired lease held by a running daemon (migration 050,
#      set to 'tradeos-vcn' on 2026-08-06 — four days before the incident).
#
# So the lease cannot be repaired into exclusion; a second process that is
# RUNNING AT ALL is one config read, one exception or one renew interval away
# from acting. The fix is to stop it existing.
#
# WHY THIS IS NOT A POSTGRES ADVISORY LOCK
# pg_advisory_lock() is session-scoped and this system reaches Postgres only
# through PostgREST, which hands every request a POOLED connection and returns
# it afterwards. A session lock taken that way is held by a connection nobody
# owns and released at a moment nobody controls — it would be a lock that
# cannot be trusted to fail, which is the defect this repo has found five
# times already. What IS atomic over PostgREST is a single conditional UPDATE:
# Postgres takes the row lock, serialises the writers, and reports how many
# rows matched. That is a compare-and-swap, and it is enough.


@dataclass
class LockResult:
    granted: bool
    code: str            # CLAIMED | STALE | FREE | HELD | LOST_RACE | UNREADABLE | OFF
    holder: str          # who holds it — this instance when granted
    detail: str


def _parse_expiry(raw) -> datetime | None:
    """The stored expiry as IST, or None when it cannot be read at all."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    # Migration 023 writes now(), which is tz-aware. A naive value is a legacy
    # or hand-edited row; assume the wall clock this system runs on rather than
    # refusing over it.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _lock_verdict(row: dict | None, me: str, now: datetime) -> tuple[bool, str, str]:
    """
    May this process claim the lock? Pure — no clock, no database, no config.

    Split out from claim_startup_lock() so the two behaviours that matter can
    be tested offline against a table of rows: a LIVE holder must refuse, and a
    genuinely stale one must not block a restart.

    Note what is deliberately NOT consulted: _is_primary(). A preference for
    which machine should normally run (migration 050) is not authority to
    barge in on one that already is — honouring it here would reproduce the
    2026-08-10 overlap exactly, since that is the path that produced it.
    """
    if row is None:
        return True, "FREE", "no lease row exists yet — claiming it"

    holder = row.get("holder") or ""
    if not holder:
        return True, "FREE", "the lease is unheld — claiming it"
    if holder == me:
        return True, "FREE", "this instance already holds the lease"

    expires = _parse_expiry(row.get("expires_at"))
    if expires is None:
        # Fail CLOSED. An unreadable expiry against a named holder cannot be
        # distinguished from a live one, and the cost of guessing wrong is two
        # daemons on a live account. Refusing costs a session in which the GTT
        # stops still protect every position, and the message below says
        # exactly how to clear it.
        return False, "UNREADABLE", (
            f"{holder} holds the lease and its expires_at is unreadable "
            f"({row.get('expires_at')!r}) — refusing to start rather than "
            f"guessing whether that process is alive. Clear it with:  UPDATE "
            f"intraday_daemon_lease SET holder='' WHERE id=1;")

    if expires > now:
        left = int((expires - now).total_seconds())
        return False, "HELD", (
            f"{holder} holds the lease for another {left}s")

    lapsed = int((now - expires).total_seconds())
    return True, "STALE", (
        f"{holder}'s lease lapsed {lapsed}s ago — claiming it")


def claim_startup_lock_with_retry(sb=None, timeout_s: int | None = None,
                                  poll_s: int | None = None) -> LockResult:
    """
    `claim_startup_lock()`, retried until it succeeds or a bounded window
    elapses — F-83, 25-Aug-2026.

    THE GAP THIS CLOSES: a single `claim_startup_lock()` call answers "may I
    claim RIGHT NOW", and every caller before this treated a refusal as
    final — the systemd timer's one attempt at 09:00, and `tradeos vcn fix`'s
    one attempt on deploy, both simply gave up and exited the moment they
    found a live holder, with nothing left running to notice if that holder
    went away a minute later. Confirmed live: after the operator stopped the
    laptop-side monitor that held the lease, the Oracle box — already exited
    from its earlier refusal, and not due to try again until the next day's
    timer — never reclaimed it. Nothing was watching for the opening.

    WHY RETRYING THE SAME CHECK IS SAFE, NOT A WEAKENING OF IT: this calls
    the unmodified `claim_startup_lock()` — the same compare-and-swap, the
    same refusal to consult `_is_primary()` migration 077 deliberately left
    out (see `_lock_verdict()`'s own docstring for why: honouring a primary
    preference against a LIVE holder is exactly the path that put two
    daemons on one account for 62 seconds on 2026-08-10). This function
    changes WHEN the check is asked, never what it is allowed to answer. It
    can only succeed once the current holder's lease has gone FREE or STALE
    on its own — a clean release, or the TTL lapsing because nothing is
    renewing it — the identical conditions a lone call already required.

    BOUNDED, NOT INDEFINITE: a holder that is genuinely alive and renewing
    (the laptop, mid-session) will never go stale, and a retry loop with no
    end would just sit there consuming a slow-timer-sized slice of nothing
    forever. `timeout_s` defaults to the lease TTL plus a margin — long
    enough to catch "already stopped, just hasn't aged out yet", short
    enough that a genuinely still-active other daemon is reported as a
    refusal rather than a silent hang. Configurable
    (`intraday_startup_claim_retry_seconds`) rather than hardcoded, matching
    every other threshold in this codebase.
    """
    # An explicit argument always wins — tests need to force a fast, tiny
    # window regardless of what system_config holds. Only an OMITTED
    # argument (the real caller's shape) falls through to config.
    if timeout_s is None:
        timeout_s = cfg_int("intraday_startup_claim_retry_seconds", _ttl() + 30)
    if poll_s is None:
        poll_s = cfg_int("intraday_startup_claim_poll_seconds", 10)

    import time
    start = time.monotonic()
    attempt = 0
    while True:
        lock = claim_startup_lock(sb)
        if lock.granted:
            if attempt:
                plural = "y" if attempt == 1 else "ies"
                logger.success(f"  startup lock: claimed after {attempt} "
                               f"retr{plural} — {lock.detail}")
            return lock

        elapsed = time.monotonic() - start
        remaining = timeout_s - elapsed
        if remaining <= 0:
            return lock

        attempt += 1
        wait = min(poll_s, remaining)
        logger.info(f"  startup lock: {lock.detail} — retrying in {wait:.0f}s "
                   f"(giving up in {remaining:.0f}s)")
        time.sleep(wait)


def claim_startup_lock(sb=None) -> LockResult:
    """
    Take the lease exclusively, or report that another daemon is running.

    Called once, at startup, BEFORE anything reads state or places an order.
    The write is a compare-and-swap: the UPDATE carries `.eq("holder", <the
    holder we just read>)`, so if another process claimed in between, zero rows
    match and this one refuses. That is the property acquire()'s unconditional
    upsert lacks, and it is why two simultaneous starts cannot both win.

    Failure is CLOSED throughout — an unreadable row, a missing table, a lost
    race and a write error all refuse. A lock that keeps trading when it cannot
    verify exclusivity is not a lock; every other guard in this file fails open
    to ACTIVE, and that is precisely how this failure survived.
    """
    if not cfg_bool("intraday_single_daemon_lock", True):
        return LockResult(True, "OFF", _INSTANCE_ID,
                          "intraday_single_daemon_lock is off — starting without "
                          "startup exclusion (migration 023 behaviour: a second "
                          "daemon runs as a standby and may act for up to one "
                          "renew interval after losing the lease)")

    sb = sb or get_supabase()
    now = _now()

    try:
        rows = (sb.table(_TABLE).select("holder,hostname,expires_at")
                  .eq("id", 1).execute().data or [])
    except Exception as e:
        return LockResult(False, "UNREADABLE", "?", (
            f"could not read {_TABLE} ({e}). Without it exclusivity cannot be "
            f"established, and starting anyway is exactly how two daemons ended "
            f"up on one live account on 2026-08-10. Apply migration 023 if this "
            f"table is missing."))

    row = rows[0] if rows else None
    may, code, detail = _lock_verdict(row, _INSTANCE_ID, now)
    if not may:
        return LockResult(False, code, (row or {}).get("holder") or "?", detail)

    patch = {
        "holder": _INSTANCE_ID,
        "hostname": socket.gethostname(),
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=_ttl())).isoformat(),
    }

    try:
        if row is None:
            # The PK on id makes a simultaneous insert fail rather than
            # duplicate. Losing that race is a refusal, same as any other.
            sb.table(_TABLE).insert({"id": 1, **patch}).execute()
            return LockResult(True, "CLAIMED", _INSTANCE_ID,
                              f"lock claimed ({detail})")

        observed = row.get("holder")
        q = sb.table(_TABLE).update(patch).eq("id", 1)
        # A held-then-freed row stores '' rather than NULL, but both shapes
        # exist in the wild and `.eq(col, None)` is not `IS NULL` in PostgREST.
        q = q.is_("holder", "null") if observed is None else q.eq("holder", observed)
        changed = q.execute().data or []
    except Exception as e:
        return LockResult(False, "LOST_RACE", (row or {}).get("holder") or "?",
                          f"could not write the lock ({e}) — refusing to start")

    if not changed:
        # The holder changed between the read and the write. Another daemon
        # claimed in that window; this is the race acquire() cannot see.
        return LockResult(False, "LOST_RACE", "?", (
            "another daemon claimed the lease between this process reading it "
            "and writing — refusing to start"))

    return LockResult(True, "CLAIMED", _INSTANCE_ID, f"lock claimed ({detail})")
