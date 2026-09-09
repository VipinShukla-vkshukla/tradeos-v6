"""
Ladder variant check — replays the current intraday exit ladder against two
candidates over real historical minute bars, validation window only
(REPLAY_DESIGN.md §8: 2026-05-04..2026-05-29). Diagnostic, not a parameter
freeze — does not touch params/frozen.json or the holdout window.

CANDIDATES
----------
atr   — trail_r replaced, per position, by an ATR-scaled equivalent
        (k * atr_pct-of-entry / risk-per-share), instead of the live book's
        flat trail_r=1.0 for every position regardless of the stock's own
        volatility.
vol   — the existing VOLUME_DECAY rung (exit_policy.py §7a) armed. Already
        built, shipped off pending calibration; no new code, just testing
        whether it should be armed.
gb50  — intraday_giveback_pct at 50% (the setting before this session's
        migration 134 tightened it to 30%), same 0.5R minimum peak.
gb_off — giveback guard disabled entirely (0%).

First run (atr/vol vs. the live 30% giveback) found both candidates
INERT — 0/238 trades differed from baseline, because the giveback guard
closes 60.5% of trades before either rung is ever reached. gb50/gb_off
test whether that guard's own setting, tightened this session on ~6 IGN
trades, holds up on a larger book-wide sample.

All reuse `evaluate_intraday_exit` unmodified — only the injected policy
dict (and the `bars=` argument step_intraday doesn't forward) differs
from the live ladder.
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import cfg_float, get_supabase
from intraday import direction as D
from intraday.exit_policy import (
    evaluate_intraday_exit, last_completed_close, load_intraday_policy,
)
from kite.kite_client import get_kite
from tools.replay.bars import BarSource
from tools.replay.detect import replay_symbol_day
from tools.replay.ladder import _fill_price
from tools.replay.universe import build_universe_for_session, prev_day_reference


@dataclass
class Trade:
    symbol: str
    engine: str
    direction: str
    entry: float
    stop: float
    r_base: float | None = None
    r_atr: float | None = None
    r_vol: float | None = None
    r_gb50: float | None = None
    r_gb_off: float | None = None
    action_base: str = ""
    action_atr: str = ""
    action_vol: str = ""
    action_gb50: str = ""
    action_gb_off: str = ""


def _qty_for(entry: float) -> int:
    cap = cfg_float("intraday_max_order_value", 6000.0)
    return max(1, int(cap / entry)) if entry > 0 else 1


def _gross_r(entry: float, exit_price: float, stop: float, d: str) -> float:
    risk = D.risk_per_share(entry, stop, d) or entry * 0.005
    return D.gain_r(entry, exit_price, risk, d)


def _walk(pos: dict, bars: list, policy: dict) -> tuple[str, float]:
    """
    Same shape as tools.replay.ladder.step_intraday, extended to forward
    `bars=` to evaluate_intraday_exit so the volume-decay rung can fire.
    Returns (terminal_action, exit_price).
    """
    pos = dict(pos)
    entry = float(pos["entry_price"])
    short = D.is_short(pos.get("direction") or "LONG")
    hwm = float(pos.get("high_water_mark") or entry)

    for i, bar in enumerate(bars, 1):
        sl = float(pos.get("active_sl") or pos.get("planned_stop") or 0)
        tgt = float(pos.get("planned_target") or 0)
        if short:
            if sl and bar.high >= sl:
                return "EXIT_STOP", sl
            if tgt and bar.low <= tgt:
                return "EXIT_TARGET", tgt
            hwm = min(hwm, float(bar.low))
        else:
            if sl and bar.low <= sl:
                return "EXIT_STOP", sl
            if tgt and bar.high >= tgt:
                return "EXIT_TARGET", tgt
            hwm = max(hwm, float(bar.high))
        pos["high_water_mark"] = hwm

        now = bar.ts
        d = evaluate_intraday_exit(
            pos, float(bar.close), policy, now=now,
            last_close=last_completed_close(bars[:i], now), bars=bars[:i])
        action = d.get("action", "HOLD")
        if action.startswith("EXIT"):
            return action, _fill_price(bar, d.get("new_sl"))
        if action == "TRAIL_SL" and d.get("new_sl"):
            pos["active_sl"] = float(d["new_sl"])
        elif action == "BOOK_PARTIAL":
            pos["partial_booked_qty"] = d.get("book_qty") or 1

    last = bars[-1] if bars else None
    return "EXIT_SQUAREOFF", (float(last.close) if last else entry)


def _daterange(start: str, end: str):
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            yield d.isoformat()
        d += timedelta(days=1)


def run(start: str = "2026-05-04", end: str = "2026-05-29",
        k_atr: float = 2.5, universe_limit: int = 40) -> list[Trade]:
    sb = get_supabase()
    src = BarSource(kite=get_kite())
    base_policy = load_intraday_policy()
    vol_policy = dict(base_policy)
    vol_policy["volume_decay_enabled"] = True
    gb50_policy = dict(base_policy)
    gb50_policy["giveback_pct"] = 50.0
    gb_off_policy = dict(base_policy)
    gb_off_policy["giveback_pct"] = 0.0

    trades: list[Trade] = []
    for day in _daterange(start, end):
        uni = build_universe_for_session(day, sb, limit=universe_limit)
        symbols = uni.symbols
        if not symbols:
            continue
        prev = prev_day_reference(day, symbols, sb)

        day_trades = 0
        for sym in symbols:
            bars = src.get(sym, day)
            if not bars:
                continue
            dets = replay_symbol_day(sym, day, bars, prev=prev.get(sym))
            if not dets:
                continue
            atr_pct = float((prev.get(sym) or {}).get("atr_pct") or 0)

            for det in dets:
                walk_bars = [b for b in bars if b.ts >= det.ts]
                if len(walk_bars) < 2:
                    continue
                pos = {
                    "entry_price": det.entry, "planned_stop": det.stop,
                    "planned_target": det.target, "direction": det.direction,
                    "active_sl": det.stop, "high_water_mark": det.entry,
                    "current_qty": _qty_for(det.entry),
                }

                atr_policy = dict(base_policy)
                risk = D.risk_per_share(det.entry, det.stop, det.direction) or det.entry * 0.005
                if atr_pct > 0 and risk > 0:
                    atr_price = atr_pct / 100.0 * det.entry
                    atr_policy["trail_r"] = (k_atr * atr_price) / risk

                a_base, x_base   = _walk(pos, walk_bars, base_policy)
                a_atr,  x_atr    = _walk(pos, walk_bars, atr_policy)
                a_vol,  x_vol    = _walk(pos, walk_bars, vol_policy)
                a_gb50, x_gb50   = _walk(pos, walk_bars, gb50_policy)
                a_gboff, x_gboff = _walk(pos, walk_bars, gb_off_policy)

                t = Trade(symbol=sym, engine=det.engine, direction=det.direction,
                          entry=det.entry, stop=det.stop,
                          r_base=_gross_r(det.entry, x_base, det.stop, det.direction),
                          r_atr=_gross_r(det.entry, x_atr, det.stop, det.direction),
                          r_vol=_gross_r(det.entry, x_vol, det.stop, det.direction),
                          r_gb50=_gross_r(det.entry, x_gb50, det.stop, det.direction),
                          r_gb_off=_gross_r(det.entry, x_gboff, det.stop, det.direction),
                          action_base=a_base, action_atr=a_atr, action_vol=a_vol,
                          action_gb50=a_gb50, action_gb_off=a_gboff)
                trades.append(t)
                day_trades += 1
        print(f"  {day}: {len(symbols)} symbols, {day_trades} trades so far this day "
              f"(cumulative {len(trades)}) — {src.coverage.line()}")
    return trades


def report(trades: list[Trade]) -> None:
    if not trades:
        print("no trades replayed")
        return
    n = len(trades)
    print()
    print("=" * 72)
    print(f"LADDER VARIANT CHECK — {n} replayed trades, gross R (no costs)")
    print("=" * 72)
    for label, key in (("baseline (live, gb=30%)", "r_base"), ("atr-trail", "r_atr"),
                       ("volume-decay armed", "r_vol"),
                       ("giveback=50% (pre-session)", "r_gb50"),
                       ("giveback=off", "r_gb_off")):
        rs = [getattr(t, key) for t in trades]
        mean = statistics.mean(rs)
        se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
        wins = sum(1 for r in rs if r > 0)
        print(f"  {label:<28} mean {mean:+.4f}R  (SE {se:.4f})  "
              f"win {wins}/{n} = {wins/n:.1%}")
    print()
    for label, key in (("atr-trail", "action_atr"), ("volume-decay", "action_vol"),
                       ("giveback=50%", "action_gb50"), ("giveback=off", "action_gb_off")):
        diff = sum(1 for t in trades if getattr(t, key) != t.action_base)
        print(f"  {label:<14} changed the terminal action on {diff}/{n} trades")
    print(f"  n={n} — INSUFFICIENT for any claim below n=100 "
          f"(REPLAY_DESIGN.md Q2 decision rule); one window, no holdout split "
          f"(the formal May validation window is unreachable — see run notes)")
    print()
    print("  baseline (gb=30%) terminal-action distribution:")
    for action, cnt in Counter(t.action_base for t in trades).most_common():
        print(f"    {action:<20} {cnt:>4}  ({cnt/n:.1%})")
    print("  giveback=off terminal-action distribution:")
    for action, cnt in Counter(t.action_gb_off for t in trades).most_common():
        print(f"    {action:<20} {cnt:>4}  ({cnt/n:.1%})")


if __name__ == "__main__":
    ts = run()
    report(ts)
