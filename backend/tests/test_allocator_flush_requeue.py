"""
allocation/allocator.py::flush() — requeue-on-failure, 2026-09-08 follow-up to
docs/FINDINGS.md's "Intraday trader review" entry (durability gap recommended
there, fix #1: "make flush() durable — do not pop self._buffer until the
insert succeeds, or re-queue rows back onto the buffer in the except branch,
so a transient failure delays a write instead of discarding it").

WHY THIS EXISTS
----------------
flush() popped self._buffer / self._pending_updates BEFORE the try, so any
exception from Supabase (PostgREST rejecting a whole payload over one bad
column — this project's own F-40 shape — or a transient network blip)
discarded up to 300 seconds of decisions across BOTH frameworks permanently,
with only a logger.error line to show for it. Real, observed symptom this
was traced from: every INTRADAY position closed 2026-09-01 through
2026-09-07 has one or more genuine DECLINE verdicts on record for the exact
(symbol, date, direction) that was actually entered, and ZERO TAKE verdicts
— the in-memory verdict that gated the entry (self._verdicts, built from the
same select() call as self._buffer) survived; its database record did not.

Fixed by RE-QUEUING on failure — same objects, not copies. That distinction
is load-bearing, not stylistic: self._collapse_state[key]["row_ref"] (the
SWING write-collapse anchor, migration 129) is the EXACT dict object sitting
in self._buffer for a not-yet-flushed candidate, mutated in place by
_write_or_collapse() when the same candidate recurs. A copy re-queued
instead of the original would silently orphan the anchor — a later cycle's
collapse decision would mutate a dict that never gets inserted. Test (c)
below exists specifically to prove this identity survives a failure.
"""
from __future__ import annotations

from tests import cfg_ctx

from allocation.allocator import Allocator
from allocation.proposal import Proposal
from allocation.policies import TAKE, DECLINE


def _proposal(**kw) -> Proposal:
    base = dict(symbol="TESTSTK", framework="SWING", product="CNC",
                entry=100.0, stop=95.0, target=115.0, quantity=10,
                source="CTL", native_rank=80.0, meta={})
    base.update(kw)
    return Proposal(**base)


def _verdict(verdict: str, edge: float = 0.05, regime_bucket: str = "NEUTRAL",
             **kw) -> dict:
    base = dict(proposal=_proposal(), verdict=verdict, edge=edge,
                hurdle=0.03, regime_bucket=regime_bucket)
    base.update(kw)
    return base


class _FlakyTable:
    """
    Enough of the Supabase query builder for insert().execute() and
    update().eq().execute(). `should_fail(op, payload, row_id=None) -> bool`
    is caller-supplied so each test controls exactly which call, in which
    batch, fails — a plain call counter can't express "row 2's update fails,
    row 1's succeeds, in the SAME flush() call" (test d). insert/update share
    one `store` dict keyed by table name, so a single flush() call's insert
    batch and its pending_updates syncs see consistent state, matching how
    the real Allocator hits the same "allocation_decisions" table for both.
    """
    def __init__(self, store: dict, should_fail, log: list):
        self._store, self._should_fail, self._log = store, should_fail, log
        self._name = None
        self._op = None
        self._payload = None
        self._eq_key = None
        self._eq_value = None

    def table(self, name):
        self._name = name
        return self

    def insert(self, rows):
        self._op, self._payload = "insert", rows
        return self

    def update(self, fields):
        self._op, self._payload = "update", fields
        return self

    def eq(self, key, value):
        self._eq_key, self._eq_value = key, value
        return self

    def execute(self):
        class _R:
            pass
        r = _R()
        if self._op == "insert":
            if self._should_fail("insert", self._payload):
                raise RuntimeError("simulated insert failure "
                                    "(e.g. PostgREST whole-payload rejection)")
            inserted = []
            for row in self._payload:
                next_id = self._store.setdefault("_next_id", [1])
                new_id = next_id[0]
                next_id[0] += 1
                stored = dict(row)
                stored["id"] = new_id
                self._store.setdefault(self._name, []).append(stored)
                inserted.append({"id": new_id})
                self._log.append(("insert", new_id))
            r.data = inserted
        else:  # update
            if self._should_fail("update", self._payload, row_id=self._eq_value):
                raise RuntimeError(f"simulated update failure for id={self._eq_value}")
            for stored in self._store.get(self._name, []):
                if stored.get(self._eq_key) == self._eq_value:
                    stored.update(self._payload)
            self._log.append(("update", self._eq_value))
            r.data = []
        return r


class _FlakySB:
    def __init__(self, store: dict, should_fail, log: list):
        self._store, self._should_fail, self._log = store, should_fail, log

    def table(self, name):
        return _FlakyTable(self._store, self._should_fail, self._log).table(name)


# ── (a) a failed insert re-queues the exact same row objects ───────────────

def test_failed_insert_requeues_the_exact_same_row_objects():
    with cfg_ctx({}):
        a = Allocator(sb=_FlakySB({}, should_fail=lambda *a, **k: True, log=[]))
        a._write_or_collapse(_verdict(DECLINE))
        a._write_or_collapse(_verdict(TAKE))
        original = list(a._buffer)

        n = a.flush()

        assert n == 0, f"a failed insert must report 0 flushed, got {n}"
        assert len(a._buffer) == 2, (
            f"a failed insert must leave both rows re-queued, got {len(a._buffer)}")
        assert all(a._buffer[i] is original[i] for i in range(2)), (
            "re-queued rows must be the SAME dict objects, not copies — required "
            "for collapse-anchor object identity (see _write_or_collapse)")


# ── (b) a retry after failure drains the buffer and writes the rows ────────

def test_retry_after_failure_drains_buffer_and_writes_rows():
    with cfg_ctx({}):
        store, log = {}, []
        calls = {"n": 0}

        def should_fail(op, payload, row_id=None):
            if op != "insert":
                return False
            calls["n"] += 1
            return calls["n"] == 1          # only the first insert attempt fails

        a = Allocator(sb=_FlakySB(store, should_fail, log))
        a._write_or_collapse(_verdict(DECLINE))
        a._write_or_collapse(_verdict(TAKE))

        n1 = a.flush()
        assert n1 == 0 and len(a._buffer) == 2

        n2 = a.flush()
        assert n2 == 2, f"the retry should succeed and report 2, got {n2}"
        assert a._buffer == [], "buffer must be empty after a successful flush"
        rows = store.get("allocation_decisions", [])
        assert len(rows) == 2, "the fake table must show both rows after the retry"
        assert {r["verdict"] for r in rows} == {DECLINE, TAKE}


# ── (c) SWING collapse-anchor object identity survives a failed flush ──────

def test_collapse_anchor_object_identity_survives_a_failed_flush_and_resolves_on_retry():
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true"}):
        store, log = {}, []
        calls = {"n": 0}

        def should_fail(op, payload, row_id=None):
            if op != "insert":
                return False
            calls["n"] += 1
            return calls["n"] == 1

        a = Allocator(sb=_FlakySB(store, should_fail, log))

        a._write_or_collapse(_verdict(DECLINE, edge=0.05))    # creates the anchor
        key = next(iter(a._collapse_state))
        anchor = a._collapse_state[key]
        anchor_row = anchor["row_ref"]
        assert anchor_row is a._buffer[0]

        n1 = a.flush()                                        # fails, re-queues
        assert n1 == 0
        assert anchor["row_ref"] is anchor_row, (
            "a failed flush must not touch the anchor's row_ref")
        assert anchor["row_id"] is None

        # A collapse decision arrives BEFORE the retry succeeds — must mutate
        # the SAME object still sitting in the re-queued self._buffer.
        a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        assert anchor["repeat_count"] == 2
        assert a._buffer[0] is anchor_row, "still one physical row, same object"
        assert a._buffer[0]["repeat_count"] == 2, (
            "the collapse must have mutated the exact re-queued object, proving "
            "the requeue preserved object identity, not a copy")

        n2 = a.flush()                                        # retry succeeds
        assert n2 == 1
        assert anchor["row_ref"] is None, "row_ref must clear once the retry lands"
        assert anchor["row_id"] is not None, "row_id must be set from the insert response"
        assert store["allocation_decisions"][0]["repeat_count"] == 2, (
            "the row actually written carries the mutated repeat_count — proof "
            "the requeue->collapse->retry path is end-to-end correct")


# ── (d) a failed pending_update is re-queued without losing a sibling ──────

def test_pending_update_failure_is_requeued_without_losing_a_sibling_success():
    store = {"allocation_decisions": [
        {"id": 1, "repeat_count": 1, "edge": 0.01, "decided_at": "t0"},
        {"id": 2, "repeat_count": 1, "edge": 0.01, "decided_at": "t0"},
    ]}
    fail_once = {2}

    def should_fail(op, payload, row_id=None):
        if op == "update" and row_id in fail_once:
            fail_once.discard(row_id)   # fails exactly once, for id=2 only
            return True
        return False

    a = Allocator(sb=_FlakySB(store, should_fail, log=[]))
    a._pending_updates[1] = {"repeat_count": 5, "edge": 0.02, "decided_at": "t1"}
    a._pending_updates[2] = {"repeat_count": 9, "edge": 0.03, "decided_at": "t1"}

    n = a.flush()

    assert n == 0   # n only reflects buffer inserts, never pending_updates
    assert 1 not in a._pending_updates, "row 1's update succeeded — must be cleared"
    assert 2 in a._pending_updates, "row 2's update failed — must be re-queued"
    assert a._pending_updates[2] == {"repeat_count": 9, "edge": 0.03, "decided_at": "t1"}, (
        "the re-queued entry must be exactly what failed, not lost or altered")
    row1 = next(r for r in store["allocation_decisions"] if r["id"] == 1)
    row2 = next(r for r in store["allocation_decisions"] if r["id"] == 2)
    assert row1["repeat_count"] == 5, "row 1's update must have actually landed"
    assert row2["repeat_count"] == 1, "row 2's update must NOT have landed — it failed"

    a.flush()   # retry: row 2's update now succeeds
    assert 2 not in a._pending_updates
    row2 = next(r for r in store["allocation_decisions"] if r["id"] == 2)
    assert row2["repeat_count"] == 9, "the retried update must land with its original fields"


TESTS = [
    ("failed insert re-queues the exact same row objects",
     test_failed_insert_requeues_the_exact_same_row_objects),
    ("retry after a failed flush drains the buffer and writes the rows",
     test_retry_after_failure_drains_buffer_and_writes_rows),
    ("SWING collapse-anchor object identity survives a failed flush and resolves on retry",
     test_collapse_anchor_object_identity_survives_a_failed_flush_and_resolves_on_retry),
    ("a failed pending_update is re-queued without losing a sibling that succeeded",
     test_pending_update_failure_is_requeued_without_losing_a_sibling_success),
]
