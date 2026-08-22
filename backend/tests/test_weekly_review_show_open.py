"""
`weekly_review.show_open()` — 22-Aug-2026.

WHY THIS EXISTS
-----------------
`.eq("source", "weekly_review")` on the read side meant this command only
ever displayed its own proposals — measured live 22-Aug-2026, 81 PENDING
rows in brain_proposals, 7 shown. discover_engines.py's own log line has
told the operator to "read them with `tradeos learn show`" since it
existed; it was never true. See show_open's own docstring for the full
account. This file checks the fix does not filter by source at all.
"""

from __future__ import annotations


class _FakeQuery:
    def __init__(self, rows, captured_filters):
        self._rows = rows
        self._filters = captured_filters

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, *a, **k):
        return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows):
        self.data = rows

    def execute(self):
        return self


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows
        self.filters: list[tuple] = []

    def table(self, name):
        assert name == "brain_proposals"
        return _FakeQuery(self._rows, self.filters)


def _row(source, target_key, confidence=0.5, proposal_type="FEATURE_FILTER"):
    return {"source": source, "target_key": target_key, "confidence": confidence,
           "proposal_type": proposal_type, "current_value": "x",
           "proposed_value": "y", "evidence": "e", "status": "PENDING"}


def test_show_open_does_not_filter_by_source():
    """The exact regression: only ever calling .eq('status', 'PENDING') on
    the read, never .eq('source', ...) — a second eq('source', ...) call
    anywhere in the built query means the bug is back."""
    from tools.weekly_review import show_open
    rows = [_row("weekly_review", "A"), _row("feature_edge_study", "B"),
           _row("discover_engines", "C"), _row("script_profiler", "D")]
    sb = _FakeSB(rows)
    rc = show_open(sb)
    assert rc == 0
    assert ("source", "weekly_review") not in sb.filters, (
        "show_open must not filter reads by source — that is the exact bug")
    assert ("status", "PENDING") in sb.filters


def test_show_open_reports_zero_cleanly_when_nothing_pending():
    from tools.weekly_review import show_open
    sb = _FakeSB([])
    assert show_open(sb) == 0


TESTS = [
    ("show_open does not filter by source", test_show_open_does_not_filter_by_source),
    ("show_open reports zero cleanly when nothing pending", test_show_open_reports_zero_cleanly_when_nothing_pending),
]
