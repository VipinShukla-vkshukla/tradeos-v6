"""
The absolute floor must not create an absorbing state (10-Aug-2026).

WHAT THIS CATCHES
-----------------
`alloc_edge_absolute_floor` refuses any proposal whose expected R does not
cover its own round trip. Correct for real capital, and the replay proved it
does what it claims: across 10 real sessions the floored policy took ZERO
trades while the unfloored one took 20 for -0.84R.

Zero trades is the trap. `priors_intraday_taken_only` builds every prior FROM
TAKEN rows, so zero trades means zero new evidence, which means the prior
freezes at whatever it was the day the floor engaged — and no amount of engine
improvement can ever show up in it, because the book is not allowed to try.
Once negative, silent forever. That is this project's own "a check that cannot
pass" failure wearing a gate's clothing.

The floor exists to protect capital. A PAPER book has none. So a live book is
blocked and a paper book explores, with BOTH verdicts recorded — the DECLINE
to `allocation_decisions` exactly as it would be live, the exploration trade to
`intraday_setups` — so the live-equivalent P&L stays computable from data on
disk. The simulation measures both systems at once.
"""

from __future__ import annotations

from tests import cfg_ctx


def _engine(verdict: dict):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng._verdicts = {("PAYTM", "MIS"): verdict}
    return eng


def _v(verdict="DECLINE", floored=True, reason="edge below the bar"):
    return {"verdict": verdict, "reason": reason,
            "hurdle_inputs": {"absolute_floor_applied": floored, "base": -1.09}}


def test_paper_explores_below_the_floor_so_the_prior_keeps_learning():
    """Without this the paper book takes nothing, writes no TAKEN rows, and its
    own prior can never move again."""
    eng = _engine(_v())
    with cfg_ctx({"alloc_live_intraday": "true",
                  "intraday_trading_mode": "PAPER",
                  "alloc_floor_blocks_paper": "false"}):
        ok, why = eng.allocator_permits("PAYTM", "MIS", "INTRADAY")
    assert ok, f"paper book blocked by the floor — the prior can never recover: {why}"
    assert "EXPLORATION" in why, f"the carve-out was silent: {why!r}"


def test_a_live_book_is_still_blocked_by_the_floor():
    """The entire point of the floor. Real capital never explores."""
    eng = _engine(_v())
    with cfg_ctx({"alloc_live_intraday": "true",
                  "intraday_trading_mode": "LIVE",
                  "alloc_floor_blocks_paper": "false"}):
        ok, why = eng.allocator_permits("PAYTM", "MIS", "INTRADAY")
    assert not ok, "a LIVE book was allowed to take a measured-losing trade"


def test_an_ordinary_decline_is_still_a_decline_in_paper():
    """The carve-out is scoped to the FLOOR only. A proposal that simply lost
    to better proposals on a healthy positive bar must still be refused, or
    the allocator stops being an allocator in paper entirely."""
    eng = _engine(_v(floored=False))
    with cfg_ctx({"alloc_live_intraday": "true",
                  "intraday_trading_mode": "PAPER",
                  "alloc_floor_blocks_paper": "false"}):
        ok, why = eng.allocator_permits("PAYTM", "MIS", "INTRADAY")
    assert not ok, f"a normal opportunity-cost decline leaked through: {why}"


def test_the_carve_out_can_be_switched_off():
    """An operator who wants paper to mirror the live block exactly — and
    accept the frozen prior — must be able to have it."""
    eng = _engine(_v())
    with cfg_ctx({"alloc_live_intraday": "true",
                  "intraday_trading_mode": "PAPER",
                  "alloc_floor_blocks_paper": "true"}):
        ok, _ = eng.allocator_permits("PAYTM", "MIS", "INTRADAY")
    assert not ok, "alloc_floor_blocks_paper=true did not restore the block"


def test_a_defer_is_never_converted_into_an_exploration_take():
    """DEFER means 'not yet, hold the slot' — it is a timing verdict, not a
    floor refusal, and turning it into a take would spend a slot the allocator
    deliberately reserved."""
    eng = _engine(_v(verdict="DEFER"))
    with cfg_ctx({"alloc_live_intraday": "true",
                  "intraday_trading_mode": "PAPER",
                  "alloc_floor_blocks_paper": "false"}):
        ok, _ = eng.allocator_permits("PAYTM", "MIS", "INTRADAY")
    assert not ok, "a DEFER was converted into an exploration entry"


TESTS = [
    ("paper explores below the floor so the prior keeps learning",
     test_paper_explores_below_the_floor_so_the_prior_keeps_learning),
    ("a live book is still blocked by the floor",
     test_a_live_book_is_still_blocked_by_the_floor),
    ("an ordinary decline is still a decline in paper",
     test_an_ordinary_decline_is_still_a_decline_in_paper),
    ("the carve-out can be switched off",
     test_the_carve_out_can_be_switched_off),
    ("a DEFER is never converted into an exploration take",
     test_a_defer_is_never_converted_into_an_exploration_take),
]
