"""
The exit ladders, stepped over bars. Both functions are IMPORTED, never copied.

THE LADDER DECIDES THE OUTCOME, NOT THE PLANNED LEVELS
-------------------------------------------------------
On the live swing book, 1 of 10 trades reached its planned target and none
reached its planned stop; the ladder decided 11 of 11 (FINDINGS.md Stage 2d).
Scoring replayed entries against planned levels would therefore measure almost
nothing about how this system actually exits. `outcomes_port.resolve` computes
the planned-level answer anyway, as the verification anchor — reporting both is
what makes a failure diagnosable (REPLAY_DESIGN §9.2).

BOTH LADDERS ARE PURE, WHICH IS WHY THIS IS POSSIBLE
------------------------------------------------------
`evaluate_exit(pos, ltp, sessions_held, policy)` — "Pure — no I/O, no mutation".
`evaluate_intraday_exit(pos, ltp, policy, now=, last_close=)` — "Pure — no I/O".
Both take an injected clock. Protect that property; it is what lets a year of
history run through the real exit logic with no database and no broker.

HIGH-WATER MARK IS COMPUTED FROM BAR HIGHS, AND THAT IS A DIVERGENCE
----------------------------------------------------------------------
The live daemon updates HWM from ticks, and the stored value **understates the
true peak on 6 of 11** live trades (F-4). This computes it from bar highs, which
is strictly more accurate than the stored column — and therefore NOT what the
live system did. Give-back will fire slightly earlier here than it did live.
Stated rather than buried, because it makes replayed give-back look marginally
better than the real thing.

THE SWING LADDER IS LONG-ONLY AND THIS IS ASSERTED
----------------------------------------------------
`evaluate_exit`'s risk line is `risk = entry - stop0 if stop0 and stop0 < entry
else None` (`position_lifecycle.py:301`) — long-only, unlike
`evaluate_intraday_exit` which is direction-aware through `intraday.direction`.
Correct while the swing book is long-only. It is exactly the shape of the
direction landmine in CLAUDE.md — a function that keeps working right up until
someone passes it a short — so `step_swing` refuses a short rather than
returning a confident wrong number.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control.position_lifecycle import evaluate_exit, load_exit_policy
from intraday.exit_policy import (
    evaluate_intraday_exit, load_intraday_policy, last_completed_close,
)
from intraday import direction as D
from intraday.strategies.base import Bar

# Actions that end the position, from `position_lifecycle.py:283-300`.
SWING_TERMINAL = {"EXIT_TARGET", "EXIT_STOP", "EXIT_TIME", "EXIT_GIVEBACK",
                  "EXIT_STALL", "EXIT_FASTFAIL", "EXIT_DETERIORATION"}


@dataclass
class LadderResult:
    """How the ladder actually closed a replayed position."""
    action: str
    reason: str
    exit_price: float
    bars_held: int
    high_water_mark: float
    partial_booked: bool = False
    trail_moves: int = 0
    log: list = field(default_factory=list)


def _fill_price(bar: Bar, trigger: float | None) -> float:
    """
    Where a fired rung actually fills.

    The rung's own trigger when it sits inside the bar's range; otherwise the
    close. **The bias has a known sign and it is conservative**: a rung whose
    trigger is outside the bar fills at the close, which is at or worse than
    where the live rung would have acted on a moving price. The replay does not
    flatter the ladder.
    """
    if trigger is not None and bar.low <= trigger <= bar.high:
        return float(trigger)
    return float(bar.close)


def step_swing(pos: dict, bars: list[Bar], policy: dict | None = None,
               sessions_held_at: dict | None = None) -> LadderResult:
    """
    Walk DAILY bars through the swing ladder. `bars` are daily, oldest first.

    Ordering per REPLAY_DESIGN §7.3 — hard levels first with the bad fill
    assumed, then HWM, then the ladder. A stop and a target inside one bar
    resolves STOP, for the same reason the outcome rule does it: a coarse bar
    cannot tell you the sequence.
    """
    if D.is_short(pos.get("direction") or "LONG"):
        raise AssertionError(
            "step_swing was passed a SHORT. `evaluate_exit`'s risk line is "
            "long-only (position_lifecycle.py:301) and would return a negative "
            "risk silently. The swing book is long-only by design; if that "
            "changes, the ladder must be made direction-aware FIRST.")

    policy = policy or load_exit_policy()
    pos = dict(pos)
    entry = float(pos["entry_price"])
    hwm = float(pos.get("high_water_mark") or entry)
    log: list = []
    partial = bool(pos.get("partial_booked_qty"))
    trail_moves = 0

    for i, bar in enumerate(bars, 1):
        sl = float(pos.get("active_sl") or pos.get("planned_stop") or 0)
        tgt = float(pos.get("planned_target") or 0)

        # 1 — hard levels first, bad fill assumed.
        if sl and bar.low <= sl:
            return LadderResult("EXIT_STOP", "stop touched", sl, i, hwm,
                                partial, trail_moves, log)
        if tgt and bar.high >= tgt:
            return LadderResult("EXIT_TARGET", "target touched", tgt, i, hwm,
                                partial, trail_moves, log)

        # 2 — HWM from the bar HIGH. See the module docstring.
        hwm = max(hwm, float(bar.high))
        pos["high_water_mark"] = hwm

        # 3 — the real ladder, on the bar's close.
        sessions = (sessions_held_at or {}).get(i, i)
        d = evaluate_exit(pos, float(bar.close), sessions, policy)
        action = d.get("action", "HOLD")
        log.append((bar.ts, action, d.get("reason", "")))

        if action in SWING_TERMINAL:
            return LadderResult(action, d.get("reason", ""),
                                _fill_price(bar, d.get("new_sl")), i, hwm,
                                partial, trail_moves, log)
        if action == "TRAIL_SL" and d.get("new_sl"):
            pos["active_sl"] = float(d["new_sl"])
            pos["trail_activated"] = True
            trail_moves += 1
        elif action == "BOOK_PARTIAL":
            pos["partial_booked_qty"] = d.get("book_qty") or 1
            partial = True

    last = bars[-1] if bars else None
    return LadderResult("OPEN", "still open at end of replayed window",
                        float(last.close) if last else entry,
                        len(bars), hwm, partial, trail_moves, log)


def step_intraday(pos: dict, bars: list[Bar],
                  policy: dict | None = None) -> LadderResult:
    """
    Walk MINUTE bars through the intraday ladder.

    `EXIT_SQUAREOFF` is terminal by design — no intraday position survives its
    own session. Direction-aware throughout, because `evaluate_intraday_exit`
    is; a short's stop sits above its entry and the bar tests reverse.
    """
    policy = policy or load_intraday_policy()
    pos = dict(pos)
    entry = float(pos["entry_price"])
    short = D.is_short(pos.get("direction") or "LONG")
    hwm = float(pos.get("high_water_mark") or entry)
    log: list = []

    for i, bar in enumerate(bars, 1):
        sl = float(pos.get("active_sl") or pos.get("planned_stop") or 0)
        tgt = float(pos.get("planned_target") or 0)

        if short:
            if sl and bar.high >= sl:
                return LadderResult("EXIT_STOP", "stop touched", sl, i, hwm, log=log)
            if tgt and bar.low <= tgt:
                return LadderResult("EXIT_TARGET", "target touched", tgt, i, hwm, log=log)
            hwm = min(hwm, float(bar.low))
        else:
            if sl and bar.low <= sl:
                return LadderResult("EXIT_STOP", "stop touched", sl, i, hwm, log=log)
            if tgt and bar.high >= tgt:
                return LadderResult("EXIT_TARGET", "target touched", tgt, i, hwm, log=log)
            hwm = max(hwm, float(bar.high))
        pos["high_water_mark"] = hwm

        now = bar.ts
        d = evaluate_intraday_exit(
            pos, float(bar.close), policy, now=now,
            last_close=last_completed_close(bars[:i], now))
        action = d.get("action", "HOLD")
        log.append((bar.ts, action, d.get("reason", "")))

        if action.startswith("EXIT") or action == "EXIT_SQUAREOFF":
            return LadderResult(action, d.get("reason", ""),
                                _fill_price(bar, d.get("new_sl")), i, hwm, log=log)
        if action == "TRAIL_SL" and d.get("new_sl"):
            pos["active_sl"] = float(d["new_sl"])
            pos["trail_activated"] = True
        elif action == "BOOK_PARTIAL":
            pos["partial_booked_qty"] = d.get("book_qty") or 1

    last = bars[-1] if bars else None
    return LadderResult("EXIT_SQUAREOFF", "end of session bars",
                        float(last.close) if last else entry,
                        len(bars), hwm, log=log)
