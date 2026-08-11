"""
Entry budgets PACED, not just counted (11-Aug-2026).

WHAT THIS CATCHES
------------------
swing_max_new_per_day / intraday_max_new_per_day counted entries but never
paced them. On 10-Aug three swing entries fired 09:36:12, 09:36:24,
09:37:03 — 51 seconds apart — because a count-only check reads 0/3, then
1/3, then 2/3 and calls each one "budget available". The whole day's swing
risk was committed in under a minute, at the open, with nothing left for
anything better later in the session.

execution.order_manager.entry_paced() is the actual pacing decision, factored
out to a pure function so it is testable without standing up either
_maybe_enter_swing()'s or _maybe_open_paper()'s full precondition chain.
_last_entry_at() / IntradayEngine._last_intraday_entry_at() are the two
timestamp sources that feed it — one per book, because swing is LIVE
(sourced from intraday_broker_log) and intraday is PAPER (sourced from
intraday_setups.ts, since paper fills never reach the broker log).

These tests pin: the pure gate blocks inside the gap and clears outside it;
the first entry of the day (no last_at) is never gated; gap_minutes<=0
restores exactly the old count-only behaviour; _last_entry_at() reads only
BUY/PLACED rows for the asked framework and returns the LATEST one, not the
first; and _last_intraday_entry_at() reads only today's TAKEN setups.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config import IST
from tests import cfg_ctx


# ── the pure decision ────────────────────────────────────────────────────

def test_blocked_inside_the_gap():
    from execution.order_manager import entry_paced
    now = datetime(2026, 8, 11, 9, 37, 0, tzinfo=IST)
    last = now - timedelta(minutes=5)
    blocked, elapsed = entry_paced(last, now, gap_minutes=20.0)
    assert blocked is True
    assert abs(elapsed - 5.0) < 1e-9


def test_clear_once_the_gap_has_elapsed():
    from execution.order_manager import entry_paced
    now = datetime(2026, 8, 11, 9, 40, 0, tzinfo=IST)
    last = now - timedelta(minutes=20, seconds=1)
    blocked, elapsed = entry_paced(last, now, gap_minutes=20.0)
    assert blocked is False
    assert elapsed > 20.0


def test_exactly_at_the_boundary_is_clear():
    """A MINIMUM gap of 20 clears once >=20 minutes have passed, not >20 —
    elapsed < gap_minutes is the block condition, so elapsed == gap_minutes
    must already be enough."""
    from execution.order_manager import entry_paced
    now = datetime(2026, 8, 11, 9, 40, 0, tzinfo=IST)
    last = now - timedelta(minutes=20)
    blocked, elapsed = entry_paced(last, now, gap_minutes=20.0)
    assert blocked is False
    assert abs(elapsed - 20.0) < 1e-9

    one_second_short = now - timedelta(minutes=19, seconds=59)
    still_blocked, _ = entry_paced(one_second_short, now, gap_minutes=20.0)
    assert still_blocked is True, "19m59s must still be inside the gap"


def test_first_entry_of_the_day_is_never_gated():
    from execution.order_manager import entry_paced
    now = datetime(2026, 8, 11, 9, 16, 0, tzinfo=IST)
    blocked, elapsed = entry_paced(None, now, gap_minutes=20.0)
    assert blocked is False, "no last_at means nothing to pace against"
    assert elapsed == float("inf")


def test_zero_gap_restores_count_only_behaviour():
    from execution.order_manager import entry_paced
    now = datetime(2026, 8, 11, 9, 37, 0, tzinfo=IST)
    last = now - timedelta(seconds=1)   # entries a second apart
    blocked, _ = entry_paced(last, now, gap_minutes=0.0)
    assert blocked is False, "gap_minutes=0 must be the exact rollback lever"


def test_the_51_second_burst_is_exactly_what_this_was_built_to_catch():
    """Regression pin for the actual 10-Aug incident."""
    from execution.order_manager import entry_paced
    e1 = datetime(2026, 8, 10, 9, 36, 12, tzinfo=IST)
    e2 = datetime(2026, 8, 10, 9, 36, 24, tzinfo=IST)
    e3 = datetime(2026, 8, 10, 9, 37, 3, tzinfo=IST)
    blocked2, _ = entry_paced(e1, e2, gap_minutes=20.0)
    blocked3, _ = entry_paced(e2, e3, gap_minutes=20.0)
    assert blocked2 is True, "the second entry, 12s after the first, must be paced"
    assert blocked3 is True, "the third entry, 39s after the second, must be paced"


# ── the DB-facing timestamp sources ─────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self): return self
    @property
    def data(self): return self._rows


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _FakeQuery(self._rows)


def test_last_entry_at_ignores_sells_and_other_frameworks():
    from execution.order_manager import _last_entry_at
    rows = [
        {"ts": "2026-08-10T09:30:00+00:00", "action": "PLACED",
         "framework": "SWING", "side": "SELL"},   # excluded: SELL
        {"ts": "2026-08-10T09:31:00+00:00", "action": "PLACED",
         "framework": "INTRADAY", "side": "BUY"},  # excluded: wrong framework
        {"ts": "2026-08-10T09:32:00+00:00", "action": "BLOCKED",
         "framework": "SWING", "side": "BUY"},     # excluded: not PLACED
        {"ts": "2026-08-10T09:36:12+00:00", "action": "PLACED",
         "framework": "SWING", "side": "BUY"},     # <- the one that should win
        {"ts": "2026-08-10T09:20:00+00:00", "action": "PLACED",
         "framework": "SWING", "side": "BUY"},     # earlier BUY, must not win
    ]
    result = _last_entry_at(_SB(rows), "SWING")
    assert result is not None
    assert result.hour == 15 and result.minute == 6, (
        f"got {result} — expected 09:36:12 UTC == 15:06:12 IST, the LATEST "
        f"qualifying BUY, not the first one in the list nor the excluded rows")


def test_last_entry_at_returns_none_with_no_qualifying_rows():
    from execution.order_manager import _last_entry_at
    result = _last_entry_at(_SB([]), "SWING")
    assert result is None


def test_last_intraday_entry_at_reads_taken_setups_only():
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb = _SB([{"ts": "2026-08-10T07:43:15+00:00"}])
    with cfg_ctx({}):
        result = eng._last_intraday_entry_at()
    assert result is not None
    assert result.hour == 13 and result.minute == 13, (
        f"got {result} — expected 07:43:15 UTC == 13:13:15 IST")


TESTS = [
    ("blocked inside the gap", test_blocked_inside_the_gap),
    ("clear once the gap has elapsed", test_clear_once_the_gap_has_elapsed),
    ("exactly at the boundary is clear",
     test_exactly_at_the_boundary_is_clear),
    ("first entry of the day is never gated", test_first_entry_of_the_day_is_never_gated),
    ("zero gap restores count-only behaviour", test_zero_gap_restores_count_only_behaviour),
    ("the 51-second burst is exactly what this was built to catch",
     test_the_51_second_burst_is_exactly_what_this_was_built_to_catch),
    ("last_entry_at ignores sells and other frameworks",
     test_last_entry_at_ignores_sells_and_other_frameworks),
    ("last_entry_at returns None with no qualifying rows",
     test_last_entry_at_returns_none_with_no_qualifying_rows),
    ("last_intraday_entry_at reads TAKEN setups only",
     test_last_intraday_entry_at_reads_taken_setups_only),
]
