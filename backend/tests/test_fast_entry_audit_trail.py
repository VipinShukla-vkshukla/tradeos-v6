"""
IGN's fast-entry approvals DO reach allocation_decisions (24-Sep-2026).

A code comment claimed `_score_proposals()` "never writes to allocation_decisions",
so a fast-path TAKE would be invisible to audits. That is not what the code does:
Allocator.select() buffers every verdict it returns and the slow-timer flush()
(run.py) writes them. Zero IGN TAKE rows on file were because no IGN TAKE had ever
been scored (0 `fast_organic` entries), not because the path drops them.

These tests pin the real behaviour so a later "optimisation" cannot quietly make
the comment true: a single-proposal select() — exactly what the fast path does —
buffers a full TAKE row, flush() inserts it, and `_score_proposals` still goes
through select().
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import cfg_ctx

_ENGINE = Path(__file__).parent.parent / "intraday" / "engine.py"


class _NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


class _Store:
    """Enough of the query builder for flush()'s insert().execute()."""
    def __init__(self):
        self.rows: list[dict] = []
        self._pending = None

    def table(self, name):
        assert name == "allocation_decisions", name
        return self

    def insert(self, rows):
        self._pending = rows
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = []
        for i, row in enumerate(self._pending):
            self.rows.append(dict(row))
            r.data.append({"id": len(self.rows)})
        return r


def _ign_take(alloc_cls, sb):
    from allocation.proposal import from_intraday
    from allocation.scoring import Prior
    from intraday.strategies.base import Setup

    s = Setup("IGNCO", "IGN", "LONG", 100.0, 99.0, 101.5, 0.7, "r", "i",
              meta={"family": "IGN", "sub_engine": "IGN"})
    p = from_intraday(s, 60)
    assert p is not None, "fixture setup must form a Proposal"
    alloc = alloc_cls(sb=sb)
    alloc._priors = {"INTRADAY/IGN": Prior("INTRADAY/IGN", 300, 0.9, 0.9, 0.02, -1.0, 1.6),
                     "INTRADAY/ALL": Prior("INTRADAY/ALL", 300, 0.9, 0.9, 0.02, -1.0, 1.6)}
    alloc._hold_days = {"INTRADAY": (1.0, 50), "SWING": (5.0, 30)}
    return alloc, p


def test_a_single_proposal_take_is_buffered_with_its_full_record():
    from allocation.allocator import Allocator
    with cfg_ctx({"alloc_hurdle_min_sample": "5", "alloc_edge_absolute_floor": "0"}):
        alloc, p = _ign_take(Allocator, _NoDB())
        verdicts = alloc.select([p], regime="RISK_ON", slots_left=4, minutes_left=200)
    assert len(verdicts) == 1 and verdicts[0]["verdict"] == "TAKE", (
        f"fixture must produce a TAKE or this proves nothing: {verdicts[0]}")
    assert len(alloc._buffer) == 1, "select() must buffer the verdict it returns"
    row = alloc._buffer[0]
    assert row["verdict"] == "TAKE" and row["symbol"] == "IGNCO"
    assert row["sub_engine"] == "IGN" and row["framework"] == "INTRADAY"
    assert row["hurdle_inputs"] is not None and row["meta"] is not None, (
        "a TAKE keeps its full record — it is a real trade's permanent audit row")


def test_flush_writes_the_buffered_take_to_allocation_decisions():
    from allocation.allocator import Allocator
    store = _Store()
    with cfg_ctx({"alloc_hurdle_min_sample": "5", "alloc_edge_absolute_floor": "0"}):
        alloc, p = _ign_take(Allocator, _NoDB())
        alloc.select([p], regime="RISK_ON", slots_left=4, minutes_left=200)
        alloc.sb = store
        n = alloc.flush()
    assert n == 1 and len(store.rows) == 1 and store.rows[0]["verdict"] == "TAKE", (
        f"the slow-timer flush must persist the fast path's TAKE: {store.rows}")
    assert alloc._buffer == []


def test_score_proposals_still_goes_through_select():
    src = _ENGINE.read_text(encoding="utf-8")
    m = re.search(r"    def _score_proposals\(.*?(?:\n    def |\Z)", src, re.DOTALL)
    assert m, "could not isolate _score_proposals()"
    body = m.group(0)
    assert "self._allocator.select(" in body, (
        "_score_proposals() no longer calls Allocator.select(), the only place a "
        "verdict is buffered for allocation_decisions — a fast-path TAKE would "
        "silently lose its audit row")
    assert "record=False" not in body and "_buffer" not in body.replace(
        "self._allocator.select(", ""), (
        "something in _score_proposals() is bypassing or draining the verdict buffer")


def test_the_slow_timer_actually_flushes_the_allocator():
    src = (Path(__file__).parent.parent / "intraday" / "run.py").read_text(encoding="utf-8")
    assert "engine._allocator.flush()" in src, (
        "run.py no longer flushes the allocator buffer, so no verdict — fast-path "
        "or ordinary — would ever reach allocation_decisions")


TESTS = [
    ("a single-proposal TAKE is buffered with its full record",
     test_a_single_proposal_take_is_buffered_with_its_full_record),
    ("flush writes the buffered TAKE to allocation_decisions",
     test_flush_writes_the_buffered_take_to_allocation_decisions),
    ("_score_proposals still goes through select()",
     test_score_proposals_still_goes_through_select),
    ("the slow timer actually flushes the allocator",
     test_the_slow_timer_actually_flushes_the_allocator),
]
