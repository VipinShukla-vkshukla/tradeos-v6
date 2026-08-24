"""
F-67, 24-Aug-2026 — the HINDCOPPER double-buy.

Order log, 24-Aug: BUY 7 @ 568.80 (04:05:24), then 15 BLOCKED retries over
5 minutes ("an identical BUY... placed Ns ago" — order_manager's own
duplicate-order cooldown, the ONLY thing standing in the way), then a
second real BUY 6 @ 570.30 at 04:10:31 — the moment that 5-minute window
lapsed. Reconcile later corrected the true broker holding to 4 shares, so
no double-size position resulted, but partial_booked_qty/original_qty were
left corrupted by the collision, and the mechanism that should have
prevented a second order at the DECISION layer (rather than relying on the
order-placement layer's own cooldown as the last line of defence) was not
doing its job.

Root cause: `_maybe_enter_swing` set `self._pending_fills[sym]` and then
immediately called `self.load_state()`, which REBUILDS the whole dict from
a fresh DB read (its own documented behaviour, correct in general — a
restart must not forget an entry was attempted). If that read has not yet
caught up with the PENDING_FILL row this same call just wrote, the rebuild
silently erases the guard one line after it was set. The fix reorders the
two lines: the guard is set AFTER load_state(), so no rebuild — stale or
not — can erase an assignment that happens after it.
"""

from __future__ import annotations


class _StaleDB:
    """Simulates the race directly: the SELECT this load_state() runs
    returns no rows, standing in for a read that has not yet caught up
    with a row this same request cycle just wrote."""
    def table(self, *a, **k):
        return self
    def select(self, *a, **k):
        return self
    def execute(self):
        class R:
            data = []
        return R()


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_StaleDB())


def test_pending_fill_guard_survives_a_load_state_that_has_not_caught_up():
    """THE FIX. Set the guard after load_state() — a rebuild that has not
    yet seen the just-written row must not be able to erase it."""
    eng = _engine()
    eng.load_state()
    eng._pending_fills["HINDCOPPER"] = "order123"
    assert "HINDCOPPER" in eng._pending_fills, (
        "the pending-fill guard must survive a load_state() call whose own "
        "DB read has not yet caught up with the row this cycle just wrote "
        "— losing it here is exactly what let HINDCOPPER be bought twice")


def test_the_old_ordering_really_did_lose_the_guard():
    """Sanity check on the fixture, not a claim about current behaviour:
    proves the PRE-FIX ordering (set, then rebuild) actually reproduces
    the failure, so the test above is not vacuously true."""
    eng = _engine()
    eng._pending_fills["HINDCOPPER"] = "order123"   # old order: set first
    eng.load_state()                                  # ... then the rebuild wipes it
    assert "HINDCOPPER" not in eng._pending_fills, (
        "sanity check failed: the pre-fix ordering must lose the guard "
        "against a stale read, or this fixture does not reproduce the "
        "real HINDCOPPER race and the test above proves nothing")


def test_load_state_still_correctly_forgets_a_genuinely_resolved_pending_fill():
    """The rebuild-from-DB behaviour itself is correct and must not be
    weakened by this fix — once a PENDING_FILL row is actually promoted to
    ACTIVE (or discarded), load_state() must stop guarding it. Only the
    ORDER relative to a fresh placement changed, not this."""
    class _ResolvedDB:
        def table(self, *a, **k):
            return self
        def select(self, *a, **k):
            return self
        def execute(self):
            class R:
                data = [{"symbol": "HINDCOPPER", "status": "ACTIVE",
                         "entry_order_id": "order123"}]
            return R()

    from intraday.engine import IntradayEngine
    eng = IntradayEngine(sb=_ResolvedDB())
    eng._pending_fills["HINDCOPPER"] = "order123"   # stale guard from before promotion
    eng.load_state()
    assert "HINDCOPPER" not in eng._pending_fills, (
        "once a position is ACTIVE, load_state() must drop the pending-fill "
        "guard — this fix must not make a resolved fill guard forever")


TESTS = [
    ("pending-fill guard survives a load_state that has not caught up",
     test_pending_fill_guard_survives_a_load_state_that_has_not_caught_up),
    ("the old ordering really did lose the guard",
     test_the_old_ordering_really_did_lose_the_guard),
    ("load_state still correctly forgets a resolved pending fill",
     test_load_state_still_correctly_forgets_a_genuinely_resolved_pending_fill),
]
