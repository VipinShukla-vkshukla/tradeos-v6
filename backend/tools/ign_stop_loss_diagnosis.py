"""
IGN's clean stop-loss hits under the REAL baseline ladder (1.5R target,
gb=30%) -- fail-fast (bad entry, stop caught it correctly) vs fail-slow
(stop placed too wide, gave back capital a tighter stop would have
saved). docs/FINDINGS.md, 23-Sep-2026.

Walks the SAME real ladder as ign_target_r_replay.py (giveback/target
active) so a trade that actually exited via TARGET or GIVEBACK is never
misclassified as a stop-out just because price happened to touch the
stop level later in the day, after the real ladder would already have
closed it.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intraday import direction as D
from intraday.exit_policy import evaluate_intraday_exit, last_completed_close, load_intraday_policy
from kite.kite_client import get_kite
from tools.ign_target_r_replay import IGN_DETECTIONS
from tools.replay.bars import BarSource


def walk_and_diagnose(pos: dict, bars: list, policy: dict) -> dict:
    """Real ladder walk (giveback/target/stop all active, same as the
    baseline variant), tracking bars-to-exit and best R reached along the
    way. Returns the real terminal action so callers can filter to
    EXIT_STOP only."""
    pos = dict(pos)
    entry = float(pos["entry_price"])
    short = D.is_short(pos.get("direction") or "LONG")
    hwm = float(pos.get("high_water_mark") or entry)
    risk = D.risk_per_share(entry, pos["planned_stop"], pos["direction"]) or entry * 0.005
    best_r = 0.0
    for i, bar in enumerate(bars, 1):
        sl = float(pos.get("active_sl") or pos.get("planned_stop") or 0)
        tgt = float(pos.get("planned_target") or 0)
        if short:
            fav = entry - bar.low
            if sl and bar.high >= sl:
                return {"action": "EXIT_STOP", "bars_to_exit": i, "mfe_r": best_r}
            if tgt and bar.low <= tgt:
                return {"action": "EXIT_TARGET", "bars_to_exit": i, "mfe_r": best_r}
            hwm = min(hwm, float(bar.low))
        else:
            fav = bar.high - entry
            if sl and bar.low <= sl:
                return {"action": "EXIT_STOP", "bars_to_exit": i, "mfe_r": best_r}
            if tgt and bar.high >= tgt:
                return {"action": "EXIT_TARGET", "bars_to_exit": i, "mfe_r": best_r}
            hwm = max(hwm, float(bar.high))
        r = fav / risk
        if r > best_r:
            best_r = r
        pos["high_water_mark"] = hwm
        now = bar.ts
        d = evaluate_intraday_exit(pos, float(bar.close), policy, now=now,
                                   last_close=last_completed_close(bars[:i], now), bars=bars[:i])
        action = d.get("action", "HOLD")
        if action.startswith("EXIT"):
            return {"action": action, "bars_to_exit": i, "mfe_r": best_r}
        if action == "TRAIL_SL" and d.get("new_sl"):
            pos["active_sl"] = float(d["new_sl"])
        elif action == "BOOK_PARTIAL":
            pos["partial_booked_qty"] = d.get("book_qty") or 1
    return {"action": "EXIT_SQUAREOFF", "bars_to_exit": len(bars), "mfe_r": best_r}


def run():
    src = BarSource(kite=get_kite())
    policy = load_intraday_policy(engine="IGN")
    results = []
    for day, sym, ts_str, direction, entry, stop in IGN_DETECTIONS:
        bars = src.get(sym, day)
        if not bars:
            continue
        det_ts = datetime.fromisoformat(ts_str)
        walk_bars = [b for b in bars if b.ts >= det_ts]
        if len(walk_bars) < 2:
            continue
        pos = {"entry_price": entry, "planned_stop": stop, "planned_target": entry + D.sign(direction)*(D.risk_per_share(entry,stop,direction) or entry*0.005)*1.5,
               "direction": direction, "active_sl": stop, "high_water_mark": entry}
        diag = walk_and_diagnose(pos, walk_bars, policy)
        if diag["action"] != "EXIT_STOP":
            continue
        results.append((sym, day, direction, diag))

    print(f"\n{'symbol':<12}{'date':<12}{'dir':<6}{'min to stop':<13}{'best R before stop'}")
    for sym, day, direction, diag in sorted(results, key=lambda r: r[3]["bars_to_exit"]):
        print(f"{sym:<12}{day:<12}{direction:<6}{diag['bars_to_exit']:<13}{diag['mfe_r']:+.3f}R")

    n = len(results)
    fast = [r for r in results if r[3]["bars_to_exit"] <= 10]
    never_positive = [r for r in results if r[3]["mfe_r"] <= 0.05]
    print(f"\n{'='*55}\nn={n} confirmed clean stop-outs (real ladder, gb=30%/1.5R)")
    print(f"  stopped within 10 min: {len(fast)}/{n}")
    print(f"  never meaningfully positive (best R <= 0.05): {len(never_positive)}/{n}")
    print(f"  those names: {[r[0] for r in never_positive]}")
    if results:
        import statistics
        mfes = [r[3]["mfe_r"] for r in results]
        print(f"  median best-R-before-stop across all {n}: {statistics.median(mfes):+.3f}R")
        print(f"  median minutes-to-stop across all {n}: {statistics.median([r[3]['bars_to_exit'] for r in results]):.0f}")


if __name__ == "__main__":
    run()
