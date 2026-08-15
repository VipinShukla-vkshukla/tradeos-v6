"""
Per-engine-family hold_days — SWING ONLY, 07-Aug-2026.

Operator's explicit requirement: this feature must not change intraday's
behaviour in any way, and anything touching shared code must be provably
isolated. `allocation/swing_hold_days.py` is a new, swing-only file that
intraday's code path never imports. `allocator.py::_hold_days_for_proposal`
is the one line that decides which book gets which figure —
`test_intraday_is_never_affected_by_the_swing_family_dict` is the proof
that INTRADAY always gets the book-pooled figure regardless of what the
swing per-family dict holds, which is the actual isolation guarantee, not
just an assertion of intent.
"""

from __future__ import annotations

from tests import cfg_ctx


# ── allocation.swing_hold_days.expected_hold_days_by_family ────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self):
        return self

    def order(self, col, *a, **k):
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        LIMIT/OFFSET with no ORDER BY has no stable row order between
        requests, so pages repeat rows and drop others — 8324 matching rows
        came back as 5000 distinct on the live book. A fake without this
        method does not fail loudly: the AttributeError is swallowed by the
        caller's non-fatal except, or turns a paged fetch into an empty list,
        which is how test_setup_rehydration silently lost 4 of 7 checks.
        """
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        return _Exec(self._rows[start:end + 1])


class _Exec:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "closed_positions", f"swing_hold_days must only read closed_positions, got {name}"
        return _FakeQuery(self._rows)


def _closed(strategy: str, hold_days: float):
    return {"strategy": strategy, "hold_days": hold_days}


def test_buckets_by_family_not_by_raw_strategy():
    """CTL and SEC are both CONTINUATION family — must pool together."""
    from allocation.swing_hold_days import expected_hold_days_by_family
    with cfg_ctx({"hold_days_min_sample": "3"}):
        sb = _FakeSB([_closed("CTL", 3.0), _closed("CTL", 5.0),
                     _closed("SEC", 4.0), _closed("SEC", 4.0)])
        out = expected_hold_days_by_family(sb)
        assert "CONTINUATION" in out, f"expected CONTINUATION family, got keys {list(out)}"
        mean, n = out["CONTINUATION"]
        assert n == 4
        assert abs(mean - 4.0) < 1e-9, f"expected mean 4.0 (3,5,4,4), got {mean}"


def test_families_below_the_floor_are_excluded():
    from allocation.swing_hold_days import expected_hold_days_by_family
    with cfg_ctx({"hold_days_min_sample": "5"}):
        sb = _FakeSB([_closed("CTL", 3.0), _closed("CTL", 5.0)])   # n=2, below floor 5
        out = expected_hold_days_by_family(sb)
        assert out == {}, (
            f"a family with only 2 closes must not be trusted at a floor of "
            f"5, got {out} — this is exactly what lets the caller fall back "
            f"to the book-pooled figure instead of a two-trade average")


def test_same_day_close_floors_at_half_a_day():
    """Matches scoring.expected_hold_days()'s own convention exactly."""
    from allocation.swing_hold_days import expected_hold_days_by_family
    with cfg_ctx({"hold_days_min_sample": "1"}):
        sb = _FakeSB([_closed("CTL", 0.0)])
        out = expected_hold_days_by_family(sb)
        mean, n = out["CONTINUATION"]
        assert mean == 0.5, f"a same-day close (0.0) must floor to 0.5, got {mean}"


# ── allocator._hold_days_for_proposal — the actual isolation guarantee ─────

def test_swing_uses_the_family_figure_when_present():
    from allocation.allocator import _hold_days_for_proposal
    days, n = _hold_days_for_proposal(
        "SWING", "CONTINUATION", book_days=1.0, book_n=0,
        swing_by_family={"CONTINUATION": (6.5, 40)})
    assert (days, n) == (6.5, 40)


def test_swing_falls_back_to_book_pooled_when_family_absent():
    from allocation.allocator import _hold_days_for_proposal
    days, n = _hold_days_for_proposal(
        "SWING", "MOM", book_days=3.2, book_n=72,
        swing_by_family={"CONTINUATION": (6.5, 40)})   # MOM not in the dict
    assert (days, n) == (3.2, 72), (
        "a family with no per-family figure yet must fall back to the "
        "book-pooled one, not error or silently use 0")


def test_intraday_is_never_affected_by_the_swing_family_dict():
    """
    THE isolation guarantee. Populate the swing per-family dict with a
    figure that would obviously change the result if it leaked into
    intraday, and confirm intraday's answer is untouched.
    """
    from allocation.allocator import _hold_days_for_proposal
    days, n = _hold_days_for_proposal(
        "INTRADAY", "VWR", book_days=1.0, book_n=500,
        swing_by_family={"VWR": (99.0, 999)})   # same source name, would be very wrong if used
    assert (days, n) == (1.0, 500), (
        f"INTRADAY must always get the book-pooled figure regardless of "
        f"the swing per-family dict's contents, got {(days, n)} — even "
        f"when a family name collides with something present there")


TESTS = [
    ("buckets by family, not by raw strategy",
     test_buckets_by_family_not_by_raw_strategy),
    ("families below the floor are excluded",
     test_families_below_the_floor_are_excluded),
    ("same-day close floors at half a day",
     test_same_day_close_floors_at_half_a_day),
    ("swing uses the family figure when present",
     test_swing_uses_the_family_figure_when_present),
    ("swing falls back to book-pooled when family absent",
     test_swing_falls_back_to_book_pooled_when_family_absent),
    ("intraday is never affected by the swing family dict",
     test_intraday_is_never_affected_by_the_swing_family_dict),
]
