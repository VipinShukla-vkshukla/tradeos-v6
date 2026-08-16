"""
A second daemon must refuse to start; a dead one must not block a restart.

WHAT THIS CATCHES
-----------------
On 2026-08-10 at 09:36 two daemons swept the same LIVE account inside 62
seconds. `intraday_broker_log` id=860 latched an IP rejection ACCOUNT-WIDE, and
ids 865/866/867/871/874 placed six real orders 12-65s later, interleaved with
nine echoes of that same latch. `_blocked_account` has one assignment site and
no reset in the whole tree, so a single process cannot produce that sequence.
A second, independent confirmation comes from a different module global:
id=878 says "an identical BUY for AUBANK was placed 22s ago", and 09:36:46−22s
is exactly id=871 — so the process that PLACED is the one that BLOCKED, and it
is not the process that echoed the latch two seconds earlier.

Every duplicate guard in `execution/order_manager.py` is a module global —
`_recent` (the 5-minute duplicate-order window), `_blocked`, `_blocked_account`
and the daily caps via `_today_totals()`. Two daemons halve all four at once,
which is the mechanism behind the CLAUDE.md landmine "PPLPHARMA sold twice this
way".

WHY THE MIGRATION-023 LEASE DID NOT CATCH IT, AND WHY THESE TESTS EXIST
-----------------------------------------------------------------------
The lease is a ROLE, not a mutex, and all three holes are in `lease.py`:

  1. `acquire()` reads the row and then upserts UNCONDITIONALLY — it never
     re-asserts what it read, so two starts in the same window both claim.
  2. The loser is only told at its next `renew()`, which `run.py` calls on a
     30s timer while the engine evaluates every 15s.
  3. `_is_primary()` (migration 050, set to 'tradeos-vcn' on 2026-08-06 — four
     days before the incident) lets a configured primary skip the deference
     check and claim a LIVE lease out from under a running daemon.

`test_a_configured_primary_may_not_barge_in_at_startup` pins hole 3 directly:
it asserts, on ONE row, that `acquire()` still claims (unchanged behaviour) and
that `claim_startup_lock()` refuses. If those two ever agree, the startup lock
has been quietly wired to the same policy that caused the incident.

WHAT MUST NOT REGRESS IN THE OTHER DIRECTION
--------------------------------------------
"A check that cannot PASS is the same defect wearing a different hat." A lock
that refuses a legitimate restart after a crash would be worse than no lock: it
would leave a live book with no daemon while looking like a safety feature.
`test_a_stale_lease_does_not_block_a_restart` is that half.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tests import cfg_ctx


# ── A one-row fake that keeps PostgREST's contract ──────────────────────────
#
# The whole point of the fix is that the WRITE is conditional, so a fake whose
# update always succeeds would test nothing. This one applies the filters and
# returns only the rows that matched — which is what makes LOST_RACE reachable.

class _Exec:
    def __init__(self, data): self.data = data


class _Table:
    def __init__(self, db):
        self.db, self.op, self.patch, self.filters = db, None, None, []

    def select(self, *a, **k):
        self.op = "select"
        return self

    def update(self, patch):
        self.op, self.patch = "update", dict(patch)
        return self

    def upsert(self, patch, on_conflict=None):
        self.op, self.patch = "upsert", dict(patch)
        return self

    def insert(self, row):
        self.op, self.patch = "insert", dict(row)
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def _matches(self, row) -> bool:
        for kind, col, val in self.filters:
            cur = row.get(col)
            if kind == "is":
                if cur is not None:
                    return False
            elif cur != val:
                return False
        return True

    def execute(self):
        db = self.db
        if self.op == "select":
            if db.row is None or not self._matches(db.row):
                return _Exec([])
            return _Exec([dict(db.row)])

        if self.op == "insert":
            if db.row is not None:
                raise Exception("duplicate key value violates unique constraint "
                                '"intraday_daemon_lease_pkey"')
            db.row = dict(self.patch)
            db.writes.append(("insert", dict(self.patch)))
            return _Exec([dict(db.row)])

        if self.op == "upsert":
            # What acquire() does today: no filters, always lands.
            db.row = {**(db.row or {}), **self.patch}
            db.writes.append(("upsert", dict(self.patch)))
            return _Exec([dict(db.row)])

        # update — the compare-and-swap
        if db.on_update:
            hook, db.on_update = db.on_update, None
            hook(db)                       # a concurrent writer lands first
        if db.row is None or not self._matches(db.row):
            db.writes.append(("update-missed", dict(self.patch)))
            return _Exec([])
        db.row.update(self.patch)
        db.writes.append(("update", dict(self.patch)))
        return _Exec([dict(db.row)])


class _DB:
    def __init__(self, row=None, on_update=None):
        self.row = dict(row) if row else None
        self.on_update = on_update
        self.writes: list = []

    def table(self, name):
        return _Table(self)


def _row(holder: str, expires_in_s: int) -> dict:
    from config import IST
    return {
        "id": 1,
        "holder": holder,
        "hostname": holder.split("-")[0],
        "expires_at": (datetime.now(IST)
                       + timedelta(seconds=expires_in_s)).isoformat(),
    }


ON = {"intraday_single_daemon_lock": "true"}


# ── The two the brief requires ──────────────────────────────────────────────

def test_a_live_lease_refuses_a_second_daemon():
    """
    The 10-Aug case: a daemon is running and renewing. A second start must
    refuse — and must leave the row alone, because a refusing process that
    still writes the holder has stolen the lease it just declined.
    """
    from intraday import lease
    db = _DB(_row("tradeos-vcn-4411-a1b2c3", +90))
    with cfg_ctx(ON):
        res = lease.claim_startup_lock(db)

    assert not res.granted, f"a live lease was claimed anyway: {res}"
    assert res.code == "HELD", res.code
    assert "tradeos-vcn-4411-a1b2c3" in res.detail, res.detail
    assert db.row["holder"] == "tradeos-vcn-4411-a1b2c3", (
        f"the refusing process overwrote the holder: {db.row['holder']}")
    assert not [w for w in db.writes if w[0] in ("update", "upsert", "insert")], (
        f"a refusal must not write: {db.writes}")


def test_a_stale_lease_does_not_block_a_restart():
    """
    The mirror, and the more dangerous failure of the two: a daemon that
    crashed at 11:00 leaves its lease behind. Refusing the restart would leave
    a live book unattended while looking like a safety feature.
    """
    from intraday import lease
    db = _DB(_row("laptop-9812-ffeedd", -300))
    with cfg_ctx(ON):
        res = lease.claim_startup_lock(db)

    assert res.granted, f"a legitimate restart was blocked: {res}"
    assert res.code == "CLAIMED", res.code
    assert "lapsed" in res.detail, res.detail
    assert db.row["holder"] == lease.instance_id(), db.row["holder"]


# ── The holes that produced the incident ────────────────────────────────────

def test_a_configured_primary_may_not_barge_in_at_startup():
    """
    Migration 050's primary override IS the 10-Aug path, and this pins it on
    one row: acquire() still claims a live foreign lease (unchanged, by
    design, for mid-run reclaim) and the startup lock refuses it. If these two
    ever agree, the lock has been wired to the policy that caused the incident.
    """
    import socket
    from intraday import lease
    me = socket.gethostname()
    cfgv = {**ON, "intraday_lease_primary_host": me}
    live = _row("other-host-7001-aabbcc", +100)

    with cfg_ctx(cfgv):
        assert lease._is_primary(), "fixture is wrong — this host is not primary"
        locked = lease.claim_startup_lock(_DB(live))
        stole = lease.acquire(_DB(live))

    assert not locked.granted, (
        "a configured primary claimed a LIVE lease at startup — this is exactly "
        "the 2026-08-10 overlap")
    assert stole.may_act, (
        "acquire() no longer claims as primary; migration 050's mid-run reclaim "
        "was changed by this work, which was not the intent")


def test_the_compare_and_swap_refuses_when_it_loses_the_race():
    """
    Two daemons starting in the same second — the race acquire()'s own
    docstring admits it cannot see. Both read a stale row; one writes first.
    The other's UPDATE carries .eq('holder', <what it read>), matches nothing,
    and must refuse rather than upsert over the winner.
    """
    from intraday import lease

    def competitor(db):
        db.row["holder"] = "the-other-daemon-5150-999999"

    db = _DB(_row("dead-1-x", -60), on_update=competitor)
    with cfg_ctx(ON):
        res = lease.claim_startup_lock(db)

    assert not res.granted, f"both daemons won the race: {res}"
    assert res.code == "LOST_RACE", res.code
    assert db.row["holder"] == "the-other-daemon-5150-999999", (
        f"the loser overwrote the winner: {db.row['holder']}")


# ── Failing closed, and staying off when told to ────────────────────────────

def test_an_unreadable_expiry_refuses_rather_than_guessing():
    """
    Every other path in lease.py fails OPEN to ACTIVE — which is how this
    survived. A holder with an expiry that cannot be parsed is
    indistinguishable from a live one, and guessing wrong costs two daemons on
    a live account.
    """
    from intraday import lease
    db = _DB({"id": 1, "holder": "someone-1-a", "expires_at": "not a timestamp"})
    with cfg_ctx(ON):
        res = lease.claim_startup_lock(db)
    assert not res.granted and res.code == "UNREADABLE", res
    assert "intraday_daemon_lease" in res.detail, (
        "a refusal the operator cannot clear is a bricked daemon — the message "
        "must carry the fix")


def test_an_unreadable_table_refuses():
    """No table, no exclusivity. Starting anyway is what happened on 10-Aug."""
    from intraday import lease

    class _Broken:
        def table(self, name): raise Exception("relation does not exist")

    with cfg_ctx(ON):
        res = lease.claim_startup_lock(_Broken())
    assert not res.granted and res.code == "UNREADABLE", res


def test_an_unheld_lease_is_free():
    """A clean shutdown writes holder='' — the next start must be instant."""
    from intraday import lease
    db = _DB({"id": 1, "holder": "", "expires_at": _row("x", -1)["expires_at"]})
    with cfg_ctx(ON):
        res = lease.claim_startup_lock(db)
    assert res.granted and res.code == "CLAIMED", res
    assert db.row["holder"] == lease.instance_id()


def test_the_switch_off_reverts_to_migration_023_exactly():
    """
    Off must be indistinguishable from the lock not existing: granted, and
    NOTHING written — a disabled guard with a side effect is not disabled.
    """
    from intraday import lease
    db = _DB(_row("tradeos-vcn-4411-a1b2c3", +90))
    with cfg_ctx({"intraday_single_daemon_lock": "false"}):
        res = lease.claim_startup_lock(db)
    assert res.granted and res.code == "OFF", res
    assert db.writes == [], f"the disabled lock still wrote: {db.writes}"
    assert db.row["holder"] == "tradeos-vcn-4411-a1b2c3"


def test_the_switch_defaults_on_when_the_row_is_missing():
    """
    A key nobody wrote must not silently disable the guard. This is the
    'silent defaults are the enemy' rule pointed at an unbounded-loss path.
    """
    from intraday import lease
    db = _DB(_row("tradeos-vcn-4411-a1b2c3", +90))
    with cfg_ctx({}):                      # no system_config row at all
        res = lease.claim_startup_lock(db)
    assert not res.granted, "the lock defaulted OFF with no config row"


# ── The verdict is pure, so pin it directly too ─────────────────────────────

def test_lock_verdict_is_pure_over_a_table_of_rows():
    from config import IST
    from intraday import lease
    now = datetime.now(IST)
    nxt = (now + timedelta(seconds=60)).isoformat()
    old = (now - timedelta(seconds=60)).isoformat()

    cases = [
        (None,                                              True,  "FREE"),
        ({"holder": "",  "expires_at": nxt},                True,  "FREE"),
        ({"holder": None, "expires_at": nxt},               True,  "FREE"),
        ({"holder": "me", "expires_at": nxt},               True,  "FREE"),
        ({"holder": "other", "expires_at": nxt},            False, "HELD"),
        ({"holder": "other", "expires_at": old},            True,  "STALE"),
        ({"holder": "other", "expires_at": None},           False, "UNREADABLE"),
    ]
    for row, want_ok, want_code in cases:
        ok, code, _ = lease._lock_verdict(row, "me", now)
        assert (ok, code) == (want_ok, want_code), f"{row} -> {(ok, code)}"


# ── F-10: the log must name the writer ──────────────────────────────────────

class _LogSB:
    """Records inserts; optionally rejects the first one the way PostgREST
    rejects an unknown column."""
    def __init__(self, reject_first_with: str | None = None):
        self.rows: list[dict] = []
        self.reject = reject_first_with

    def table(self, name):
        return self

    def insert(self, row):
        self._pending = dict(row)
        return self

    def execute(self):
        if self.reject:
            msg, self.reject = self.reject, None
            raise Exception(msg)
        self.rows.append(self._pending)
        return _Exec([self._pending])


def _req():
    from execution.order_manager import OrderRequest
    return OrderRequest("PPLPHARMA", "SELL", 7, "LIMIT", 437.2)


def test_the_broker_log_records_host_and_pid():
    """
    §4 could PROVE two writers and could not NAME either. Without these two
    columns the next occurrence is inferred from the behaviour of module
    globals all over again.
    """
    import os
    import socket
    from execution import order_manager as om
    sb = _LogSB()
    om._log(sb, _req(), "PLACED", "251110000123", "BOOK_PARTIAL", "SWING")
    assert len(sb.rows) == 1, sb.rows
    assert sb.rows[0]["host"] == socket.gethostname()
    assert sb.rows[0]["pid"] == os.getpid()


def test_an_unmigrated_column_costs_the_attribution_not_the_row():
    """
    "PostgREST fails the WHOLE update on one unknown column." This table is the
    money trail — the only record that an order was attempted. Adding
    attribution must never be able to delete the thing it attributes, and _log
    swallows its exception to logger.debug, so the loss would be silent.
    """
    from execution import order_manager as om
    sb = _LogSB(reject_first_with=
                "{'code': 'PGRST204', 'message': \"Could not find the 'host' "
                "column of 'intraday_broker_log' in the schema cache\"}")
    om._log(sb, _req(), "PLACED", "251110000123", "BOOK_PARTIAL", "SWING")
    assert len(sb.rows) == 1, f"the row was lost entirely: {sb.rows}"
    assert "host" not in sb.rows[0] and "pid" not in sb.rows[0]
    assert sb.rows[0]["symbol"] == "PPLPHARMA"
    assert sb.rows[0]["detail"] == "BOOK_PARTIAL"


def test_a_network_error_does_not_write_the_row_twice():
    """
    The retry above is gated on the error naming a column. An ordinary
    timeout must NOT be retried — an order log written twice is a phantom
    second attempt in the audit trail this whole entry is reasoning from.
    """
    from execution import order_manager as om
    sb = _LogSB(reject_first_with="HTTPSConnectionPool: Read timed out")
    om._log(sb, _req(), "PLACED", "251110000123", "BOOK_PARTIAL", "SWING")
    assert sb.rows == [], f"a timeout was retried and duplicated: {sb.rows}"


# ── The call site, not just the function ────────────────────────────────────

def test_the_daemon_claims_the_lock_before_it_can_act():
    """
    A correct guard proves nothing about its callers — the direction-aware
    shorting work found that same gap four separate times in one feature, and
    the cure recorded in CLAUDE.md is to check the literal CALL SITE.

    Here the call site has an ordering requirement as well as an existence
    one: a lock claimed after load_state(), after the universe, or after the
    first cycle would be decoration. So this pins the order in run.main()
    itself: claim, then acquire, then load state, then ever cycle.
    """
    import inspect
    from intraday import run
    src = inspect.getsource(run.main)

    claim = src.find("claim_startup_lock")
    assert claim >= 0, "run.main() no longer claims the startup lock at all"

    guarded = src.find("if not lock.granted")
    assert guarded > claim, "the lock result is never tested"
    tail = src[guarded:guarded + 2000]
    assert "return" in tail, "run.main() does not return when the lock is refused"

    for later in ("lease.acquire", "engine.load_state()", "engine.cycle("):
        at = src.find(later)
        assert at > claim, (
            f"run.main() reaches {later} before claiming the startup lock — a "
            f"second daemon would already be acting")


TESTS = [
    ("a live lease refuses a second daemon", test_a_live_lease_refuses_a_second_daemon),
    ("the daemon claims the lock before it can act",
     test_the_daemon_claims_the_lock_before_it_can_act),
    ("a stale lease does not block a restart", test_a_stale_lease_does_not_block_a_restart),
    ("a configured primary may not barge in at startup",
     test_a_configured_primary_may_not_barge_in_at_startup),
    ("the compare-and-swap refuses when it loses the race",
     test_the_compare_and_swap_refuses_when_it_loses_the_race),
    ("an unreadable expiry refuses rather than guessing",
     test_an_unreadable_expiry_refuses_rather_than_guessing),
    ("an unreadable lease table refuses", test_an_unreadable_table_refuses),
    ("an unheld lease is free", test_an_unheld_lease_is_free),
    ("the switch off reverts to migration 023 exactly",
     test_the_switch_off_reverts_to_migration_023_exactly),
    ("the switch defaults on when the row is missing",
     test_the_switch_defaults_on_when_the_row_is_missing),
    ("lock verdict is pure over a table of rows",
     test_lock_verdict_is_pure_over_a_table_of_rows),
    ("the broker log records host and pid", test_the_broker_log_records_host_and_pid),
    ("an unmigrated column costs the attribution, not the row",
     test_an_unmigrated_column_costs_the_attribution_not_the_row),
    ("a network error does not write the row twice",
     test_a_network_error_does_not_write_the_row_twice),
]
