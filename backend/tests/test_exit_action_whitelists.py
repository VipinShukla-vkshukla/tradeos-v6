"""
Every action the ladder can return must reach all three consumers.

`evaluate_exit` returns EXIT_INVALIDATED for a SWING position whose thesis
breaks before it ever proved itself (position_lifecycle.py's rung 2b2, live
since swing_early_invalidation_enabled was armed). The batch path then dropped
it three times over: the exit_signal recorder, the order placer and the
SELLABLE alert list all listed their actions as literals, and none of the three
literals contained it. The verdict was computed and discarded — no record, no
order, no alert, position still open.

tools/health.py's own check could not see this: it exempted EXIT_INVALIDATED as
"intraday-only" while the swing ladder was emitting it.

One definition now, four readers (the three above plus the daemon). These tests
fail if a reader ever re-declares its own list.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import cfg_ctx

ROOT = Path(__file__).resolve().parent.parent


def _broken_sig() -> dict:
    """Inputs assess_trend() reads as BROKEN — same shape as the E4 fixture."""
    return {"dist_sma50": -8.0, "rsi_daily": 28.0, "adx": 12.0,
            "vol_ratio": 0.4, "rs_vs_nifty": -3.0, "sector_rank_at_entry": 18}


def _pos(entry=100.0, stop=94.0) -> dict:
    return {"symbol": "X", "entry_price": entry, "active_sl": stop,
            "planned_stop": stop, "high_water_mark": entry, "current_qty": 10,
            "actual_qty": 10, "framework": "SWING", "direction": "LONG",
            "strategy": "CTL", "sector": "metals & mining"}


def _policy() -> dict:
    from control.position_lifecycle import load_exit_policy
    with cfg_ctx({}):
        return load_exit_policy()


def test_early_invalidation_verdict_is_sell_capable():
    """The verdict the swing ladder actually produces must be one every
    consumer can act on. This is the defect, stated as a test."""
    from control.position_lifecycle import (EXIT_ACTIONS_FULL, EXIT_ACTIONS_SELL,
                                            evaluate_exit)
    entry, stop = 100.0, 94.0
    ltp = entry + 0.2 * (entry - stop)          # +0.2R, below the 1.0R gate
    policy = _policy()
    policy["_trend_ctx"] = {"X": _broken_sig()}
    with cfg_ctx({"swing_early_invalidation_enabled": "true"}):
        d = evaluate_exit(_pos(entry, stop), ltp, 3, policy)
    assert d["action"] == "EXIT_INVALIDATED", d["action"]
    assert d["action"] in EXIT_ACTIONS_FULL, (
        f"{d['action']} is returned by the ladder but the exit_signal recorder "
        f"cannot write it: {EXIT_ACTIONS_FULL}")
    assert d["action"] in EXIT_ACTIONS_SELL, (
        f"{d['action']} sells a position but the order placer and the SELLABLE "
        f"alert list cannot see it: {EXIT_ACTIONS_SELL}")


def test_partial_book_sells_but_is_not_a_full_exit():
    """BOOK_PARTIAL places an order and must alert, but it does not write
    exit_signal — the position stays open."""
    from control.position_lifecycle import EXIT_ACTIONS_FULL, EXIT_ACTIONS_SELL
    assert "BOOK_PARTIAL" in EXIT_ACTIONS_SELL
    assert "BOOK_PARTIAL" not in EXIT_ACTIONS_FULL


def test_daemon_whitelist_is_the_same_set_plus_squareoff():
    """The daemon and the batch path are two callers of one decision function.
    EXIT_SQUAREOFF is the only action genuinely intraday-only."""
    from control.position_lifecycle import EXIT_ACTIONS_SELL
    from intraday.engine import DAEMON_EXIT_ACTIONS
    assert set(DAEMON_EXIT_ACTIONS) - set(EXIT_ACTIONS_SELL) == {"EXIT_SQUAREOFF"}, (
        f"daemon extras: {sorted(set(DAEMON_EXIT_ACTIONS) - set(EXIT_ACTIONS_SELL))}")
    assert not set(EXIT_ACTIONS_SELL) - set(DAEMON_EXIT_ACTIONS), (
        "the daemon cannot act on something the batch path can: "
        f"{sorted(set(EXIT_ACTIONS_SELL) - set(DAEMON_EXIT_ACTIONS))}")


def test_no_reader_redeclares_its_own_action_list():
    """The regression guard. Three literals drifted apart once; a fourth reader
    writing its own tuple is how it happens again."""
    bad = []
    for rel in ("control/position_lifecycle.py", "intraday/engine.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'\(\s*"EXIT_[A-Z_]+"(?:\s*,\s*\n?\s*"(?:EXIT_[A-Z_]+|BOOK_PARTIAL)")+\s*,?\s*\)',
                             src):
            seg = m.group(0)
            if len(re.findall(r'"(?:EXIT_[A-Z_]+|BOOK_PARTIAL)"', seg)) < 3:
                continue
            line = src[:m.start()].count("\n") + 1
            if rel.endswith("position_lifecycle.py") and line < 80:
                continue          # the one definition itself
            bad.append(f"{rel}:{line}")
    assert not bad, ("action lists must reference EXIT_ACTIONS_FULL / "
                     f"EXIT_ACTIONS_SELL, not a literal tuple: {bad}")


TESTS = [
    ("early-invalidation verdict is sell-capable", test_early_invalidation_verdict_is_sell_capable),
    ("partial book sells but is not a full exit", test_partial_book_sells_but_is_not_a_full_exit),
    ("daemon whitelist is the same set plus squareoff", test_daemon_whitelist_is_the_same_set_plus_squareoff),
    ("no reader redeclares its own action list", test_no_reader_redeclares_its_own_action_list),
]
