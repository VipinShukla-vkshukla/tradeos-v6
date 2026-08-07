"""
`_track_excursion` runs every 15s for every open position (both books) and
is the only live-cycle writer of `current_price`/`pnl_pct`/excursion stats —
but until now it never wrote `unrealized_pnl` or `current_value`, the two
fields `PositionsTab.tsx` reads for the dashboard's primary rupee P&L
figure. Those were only ever written once a day, by
`manage_open_positions()` at EOD (control/position_lifecycle.py). So the
dashboard's percentage P&L moved live all session while the rupee figure
sat frozen at whatever it was at the previous evening's batch run, for both
books, until the next EOD run overwrote it.

This exercises the real function against a fake position row and asserts
`unrealized_pnl`/`current_value` land in the actual database payload — not
just that the function runs without raising — for both a LONG and a SHORT,
since this path serves intraday's SDN engine as well as swing's LONG-only
book, and a naive `(ltp - entry) * qty` is the wrong sign for a short.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, sink: list, table: str, upd: dict):
        self._sink = sink
        self._table = table
        self._upd = dict(upd)
        self._filters: dict = {}

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._sink.append({"table": self._table, "update": self._upd,
                            "filters": dict(self._filters)})
        return type("R", (), {"data": []})()


class _FakeTable:
    def __init__(self, sink: list, name: str):
        self._sink = sink
        self._name = name

    def update(self, upd: dict):
        return _FakeQuery(self._sink, self._name, upd)


class _FakeDB:
    def __init__(self):
        self.calls: list = []

    def table(self, name: str):
        return _FakeTable(self.calls, name)


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_FakeDB())


def _position(**over):
    p = {
        "symbol": "RELIANCE",
        "product": "MIS",
        "framework": "INTRADAY",
        "direction": "LONG",
        "entry_price": 100.0,
        "current_qty": 10,
        "high_water_mark": 100.0,
        "max_favorable_excursion": 0.0,
        "max_adverse_excursion": 0.0,
        "planned_stop": 98.0,
    }
    p.update(over)
    return p


def test_long_writes_positive_unrealized_pnl_on_a_gain():
    with cfg_ctx():
        eng = _engine()
        p = _position()
        eng._track_excursion(p, 102.0)   # +2 per share, qty 10 -> +20
        assert eng.sb.calls, "no update reached the database — unrealized_pnl silently not written"
        upd = eng.sb.calls[-1]["update"]
        assert "unrealized_pnl" in upd, "unrealized_pnl missing from the write payload"
        assert upd["unrealized_pnl"] == 20.0, f"expected +20.0, got {upd['unrealized_pnl']}"
        assert upd["current_value"] == 1020.0, f"expected 1020.0, got {upd['current_value']}"


def test_long_writes_negative_unrealized_pnl_on_a_loss():
    with cfg_ctx():
        eng = _engine()
        p = _position()
        eng._track_excursion(p, 98.5)    # -1.5 per share, qty 10 -> -15
        upd = eng.sb.calls[-1]["update"]
        assert upd["unrealized_pnl"] == -15.0, f"expected -15.0, got {upd['unrealized_pnl']}"


def test_short_sign_is_inverted_not_naive_ltp_minus_entry():
    """A short in profit (price fell) must show a POSITIVE unrealized_pnl."""
    with cfg_ctx():
        eng = _engine()
        p = _position(direction="SHORT", planned_stop=102.0)
        eng._track_excursion(p, 97.0)    # price fell 3, short is UP 3/share, qty 10 -> +30
        upd = eng.sb.calls[-1]["update"]
        assert upd["unrealized_pnl"] == 30.0, (
            f"expected +30.0 (short profits when price falls), got "
            f"{upd['unrealized_pnl']} — naive (ltp-entry)*qty would wrongly give -30.0")


def test_falls_back_to_actual_qty_when_current_qty_absent():
    with cfg_ctx():
        eng = _engine()
        p = _position()
        del p["current_qty"]
        p["actual_qty"] = 5
        eng._track_excursion(p, 102.0)   # +2/share, qty 5 -> +10
        upd = eng.sb.calls[-1]["update"]
        assert upd["unrealized_pnl"] == 10.0, f"expected +10.0, got {upd['unrealized_pnl']}"


TESTS = [
    ("long position writes positive unrealized_pnl on a gain",
     test_long_writes_positive_unrealized_pnl_on_a_gain),
    ("long position writes negative unrealized_pnl on a loss",
     test_long_writes_negative_unrealized_pnl_on_a_loss),
    ("short position's unrealized_pnl sign is direction-aware",
     test_short_sign_is_inverted_not_naive_ltp_minus_entry),
    ("qty falls back to actual_qty when current_qty is absent",
     test_falls_back_to_actual_qty_when_current_qty_absent),
]
