"""
F-67, 24-Aug-2026 — TWO real incidents, same shape, same root cause,
discovered three days apart.

HINDCOPPER, 24-Aug: BUY 7 @ 568.80 (04:05:24), then 15 BLOCKED retries
over 5 minutes ("an identical BUY... placed Ns ago" — order_manager's own
duplicate-order cooldown, the ONLY thing standing in the way), then a
second real BUY 6 @ 570.30 at 04:10:31 — the moment that 5-minute window
lapsed. Reconcile corrected the true broker holding to 4 shares.

HAL, 21-Aug — found LATER, by the standing health check Stage E3 built
specifically because one fix is not a guarantee the shape never recurs
(F-70). Same signature, three days EARLIER: BUY 1 @ 5021.80 (08:13:57),
10 blocked retries, BUY 1 @ 5020.70 (08:19:08) — 311 seconds later, again
the moment the cooldown lapsed. Unlike HINDCOPPER, HAL's own small
1-share orders both filled cleanly: `current_qty=actual_qty=kite_qty=2,
MATCHED` against the broker — a real, live position carrying roughly
double its intended risk allocation, retained rather than trimmed at the
operator's own explicit instruction (docs/FINDINGS.md F-70 §1).

Root cause, identical for both: `_maybe_enter_swing` set
`self._pending_fills[sym]` and then immediately called `self.load_state()`,
which REBUILDS the whole dict from a fresh DB read (its own documented
behaviour, correct in general — a restart must not forget an entry was
attempted). If that read has not yet caught up with the PENDING_FILL row
this same call just wrote, the rebuild silently erases the guard one line
after it was set. The fix reorders the two lines: the guard is set AFTER
`load_state()`, so no rebuild — stale or not — can erase an assignment
that happens after it.

THE MECHANISM HAS NO DEPENDENCY ON SYMBOL, PRICE OR QUANTITY — which is
exactly why the same two-line reorder closes both incidents. Parametrized
below over both real symbols rather than proved for one and assumed for
the other, so "will this happen again" has an actual test behind the
answer, not just an inference from one incident to a different one.
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


REAL_INCIDENT_SYMBOLS = ("HINDCOPPER", "HAL")


def test_pending_fill_guard_survives_a_load_state_that_has_not_caught_up():
    """THE FIX, over BOTH real incident symbols. Set the guard after
    load_state() — a rebuild that has not yet seen the just-written row
    must not be able to erase it, for either name."""
    for sym in REAL_INCIDENT_SYMBOLS:
        eng = _engine()
        eng.load_state()
        eng._pending_fills[sym] = "order123"
        assert sym in eng._pending_fills, (
            f"the pending-fill guard must survive a load_state() call "
            f"whose own DB read has not yet caught up with the row this "
            f"cycle just wrote — losing it here is exactly what let "
            f"{sym} be bought twice")


def test_the_old_ordering_really_did_lose_the_guard():
    """Sanity check on the fixture, not a claim about current behaviour:
    proves the PRE-FIX ordering (set, then rebuild) actually reproduces
    the failure for both real symbols, so the test above is not
    vacuously true for either."""
    for sym in REAL_INCIDENT_SYMBOLS:
        eng = _engine()
        eng._pending_fills[sym] = "order123"   # old order: set first
        eng.load_state()                         # ... then the rebuild wipes it
        assert sym not in eng._pending_fills, (
            f"sanity check failed: the pre-fix ordering must lose the "
            f"guard against a stale read for {sym}, or this fixture does "
            f"not reproduce the real race and the test above proves "
            f"nothing")


def test_load_state_still_correctly_forgets_a_genuinely_resolved_pending_fill():
    """The rebuild-from-DB behaviour itself is correct and must not be
    weakened by this fix — once a PENDING_FILL row is actually promoted to
    ACTIVE (or discarded), load_state() must stop guarding it. Only the
    ORDER relative to a fresh placement changed, not this. Checked for
    both real symbols."""
    for sym in REAL_INCIDENT_SYMBOLS:
        class _ResolvedDB:
            def table(self, *a, **k):
                return self
            def select(self, *a, **k):
                return self
            def execute(self):
                class R:
                    data = [{"symbol": sym, "status": "ACTIVE",
                             "entry_order_id": "order123"}]
                return R()

        from intraday.engine import IntradayEngine
        eng = IntradayEngine(sb=_ResolvedDB())
        eng._pending_fills[sym] = "order123"   # stale guard from before promotion
        eng.load_state()
        assert sym not in eng._pending_fills, (
            f"once {sym} is ACTIVE, load_state() must drop the "
            f"pending-fill guard — this fix must not make a resolved "
            f"fill guard forever")


TESTS = [
    ("pending-fill guard survives a load_state that has not caught up "
     "(HINDCOPPER + HAL)",
     test_pending_fill_guard_survives_a_load_state_that_has_not_caught_up),
    ("the old ordering really did lose the guard (HINDCOPPER + HAL)",
     test_the_old_ordering_really_did_lose_the_guard),
    ("load_state still correctly forgets a resolved pending fill "
     "(HINDCOPPER + HAL)",
     test_load_state_still_correctly_forgets_a_genuinely_resolved_pending_fill),
]
