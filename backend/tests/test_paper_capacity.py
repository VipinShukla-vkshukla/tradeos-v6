"""
`execution.paper_broker.capacity()` used to read its own `paper_starting_
capital` key — introduced 31-Jul-2026, before `config.capital_for()`'s
book-sleeve mechanism existed at all (05/06-Aug), and never migrated once it
did. Confirmed live, 07-Aug-2026: `paper_starting_capital=20000` sat next to
`intraday_capital=100000` — the dashboard's own "Capital" field — and nothing
on the panel, in the logs, or in the code connected the two, so paper
deployment was silently capped at a fifth of what the operator could see was
configured.

Fixed to read `capital_for(framework)` directly — the same number the live
sizing formula uses — so the two can never disagree again. Also scoped
`deployed` to the framework being asked about; it previously summed BOTH
books' paper positions against one book's cap, which combined with the
sleeve fix above would otherwise have made a framework-specific cap get
compared against a cross-book-pooled deployed figure — the same shape as
every other book-pooling bug found this session, and currently dormant only
because swing has never run in PAPER mode.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._data})()


class _FakeDB:
    def __init__(self, open_positions: list[dict]):
        self._rows = open_positions

    def table(self, name: str):
        assert name == "open_positions"
        return _FakeQuery(self._rows)


def _position(framework: str, invested: float):
    return {"symbol": "X", "invested_value": invested, "framework": framework}


def test_capacity_reads_the_same_sleeve_sizing_uses():
    from execution import paper_broker
    with cfg_ctx({"intraday_capital": "100000"}):
        db = _FakeDB([])
        allowed, why, left = paper_broker.capacity("INTRADAY", db)
        assert allowed, f"expected room with nothing deployed, got: {why}"
        assert left == 100000.0, (
            f"capacity() must read capital_for('INTRADAY') (100000), got "
            f"left={left} — it drifted back to a second, independent number")


def test_capacity_shrinks_when_intraday_capital_is_lowered():
    """The whole point: one number, so changing it here changes capacity too."""
    from execution import paper_broker
    with cfg_ctx({"intraday_capital": "20000"}):
        db = _FakeDB([])
        _, _, left = paper_broker.capacity("INTRADAY", db)
        assert left == 20000.0, (
            f"expected capacity to track intraday_capital=20000, got left={left}")


def test_deployed_value_is_scoped_to_its_own_framework():
    """A swing PAPER position (hypothetical — swing has never run in PAPER
    mode, but the parameter exists) must not eat into intraday's cap."""
    from execution import paper_broker
    with cfg_ctx({"intraday_capital": "100000"}):
        db = _FakeDB([_position("SWING", 90000.0), _position("INTRADAY", 5000.0)])
        allowed, why, left = paper_broker.capacity("INTRADAY", db)
        assert left == 95000.0, (
            f"expected 100000 - 5000 (INTRADAY's own deployment only), got "
            f"left={left} — the other book's paper position is leaking into "
            f"this book's capacity")
        assert allowed, f"expected room left, got: {why}"


def test_capacity_exhausted_reports_the_correct_deployed_figure():
    from execution import paper_broker
    with cfg_ctx({"intraday_capital": "10000"}):
        db = _FakeDB([_position("INTRADAY", 10000.0)])
        allowed, why, left = paper_broker.capacity("INTRADAY", db)
        assert not allowed, "expected capital exhausted, got allowed"
        assert "10,000" in why or "10000" in why, (
            f"reason should name the deployed figure against the sleeve, got: {why!r}")


TESTS = [
    ("capacity reads the same sleeve sizing uses",
     test_capacity_reads_the_same_sleeve_sizing_uses),
    ("capacity shrinks when intraday_capital is lowered",
     test_capacity_shrinks_when_intraday_capital_is_lowered),
    ("deployed value is scoped to its own framework",
     test_deployed_value_is_scoped_to_its_own_framework),
    ("capacity exhausted reports the correct deployed figure",
     test_capacity_exhausted_reports_the_correct_deployed_figure),
]
