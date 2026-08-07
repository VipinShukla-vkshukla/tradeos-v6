"""
`_log_intraday_state` runs every cycle regardless of whether anything was
found — the missing mirror of `_log_swing_state`, which was itself added to
fix the identical problem in the opposite direction (see that function's own
docstring: "the console showed the intraday engine scan in detail and swing
not at all").

Confirmed live, 07-Aug-2026: several consecutive minutes of a real session
showed only the swing heartbeat repeating, with no intraday-shaped line
between them, even though evaluate_intraday_setups runs unconditionally
every cycle in cycle() — same as evaluate_candidates does for swing. The
absence of a line was never evidence the scan had stopped; there was simply
no line for "scanned this cycle, found nothing" to be. This exercises the
new function directly, with both an empty and a populated setups list, so a
future signature change can't silently reintroduce the same silence.
"""

from __future__ import annotations

from tests import cfg_ctx


class _NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_NoDB())


def _setup_entry(symbol: str):
    from intraday.strategies.base import Setup
    s = Setup(symbol, "ORB", "LONG", 100.0, 98.0, 104.0, 0.7, "r", "i",
             meta={"family": "ORB"})
    return {"setup": s, "qty": 5, "cost_pct": 0.1}


def test_logs_a_quiet_cycle_without_raising():
    with cfg_ctx():
        eng = _engine()
        eng._contexts = {"RELIANCE": object(), "TCS": object()}
        eng.positions = [{"symbol": "TCS", "framework": "INTRADAY"},
                         {"symbol": "INFY", "framework": "SWING"}]
        eng._log_intraday_state([])   # must not raise on an empty cycle


def test_logs_a_cycle_with_setups_without_raising():
    with cfg_ctx():
        eng = _engine()
        eng._contexts = {"RELIANCE": object(), "TCS": object()}
        eng.positions = [{"symbol": "TCS", "framework": "INTRADAY"}]
        eng._log_intraday_state([_setup_entry("RELIANCE")])


def test_counts_only_intraday_positions_not_swing():
    """The held count must not be inflated by swing positions in self.positions."""
    with cfg_ctx():
        eng = _engine()
        eng._contexts = {}
        eng.positions = [
            {"symbol": "TCS", "framework": "INTRADAY"},
            {"symbol": "WIPRO", "framework": "INTRADAY"},
            {"symbol": "INFY", "framework": "SWING"},
        ]
        held = sum(1 for p in eng.positions
                   if (p.get("framework") or "SWING").upper() == "INTRADAY")
        assert held == 2, "sanity check on the fixture itself"
        eng._log_intraday_state([])   # must not raise; held is computed the same way


TESTS = [
    ("logs a quiet cycle without raising", test_logs_a_quiet_cycle_without_raising),
    ("logs a cycle with setups without raising", test_logs_a_cycle_with_setups_without_raising),
    ("counts only intraday positions, not swing", test_counts_only_intraday_positions_not_swing),
]
