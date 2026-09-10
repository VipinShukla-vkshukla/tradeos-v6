"""
IGN stop-variant check — does the initial structural stop (swing low/high over
`ign_stop_lookback_bars` minutes, buffered by `ign_stop_buffer_pct`) sit too
tight on an already-extended entry, the way it did on both of IGN's live
losses on 10-Sep-2026 (TEGA, REDINGTON — both STOP_LOSS_HIT, entered within
1-1.5% of the day's high, both stocks still closed the day up)?

WHY THIS IS A DIFFERENT QUESTION THAN THE GIVEBACK/TRAIL/VOLUME-DECAY WORK
---------------------------------------------------------------------------
`ladder_variant_check.py` (abef966, b5f962e) tests rungs 5b/6/7a of the exit
ladder — giveback, ATR-trail, volume-decay — all of which only ever fire on a
position that is ALREADY IN PROFIT. Both of IGN's live losses were
STOP_LOSS_HIT before either trade was ever green; nothing downstream of the
initial stop was reached. This checks the initial stop itself — a genuinely
separate lever, upstream of everything that check touched.

WHAT IS REUSED, WHAT IS REPLICATED
------------------------------------
`replay_symbol_day` (detect.py) is reused UNMODIFIED to generate the baseline
IGN detection population — same bars, same context, same dedup as the
giveback work. `_walk`/`_gross_r`/`_qty_for` (ladder_variant_check.py) are
reused UNMODIFIED for the forward walk once a variant stop is chosen — the
exit ladder downstream of entry (giveback/trail/time-stop) is identical
across every variant tested here; only the INITIAL stop differs, so only the
initial stop should be reimplemented.

The structural-stop arithmetic itself (`ignition.py::_long`/`_short`) IS
replicated rather than imported, because the whole point is evaluating INPUTS
the live function does not expose as parameters (lookback, buffer, an ATR
floor). Reaching that behaviour by monkeypatching `system_config` mid-run
would touch a LIVE trading system's actual config table — precisely the
"one test's switches leak into the next" risk `cfg_ctx()` exists to prevent
in `tools/verify.py`, and there is no equivalent guard for a script hitting
the real Supabase project. Recomputing from the same bars with literal
parameter values touches no global state and cannot leak into a concurrent
live cycle. THE BASELINE VARIANT IS ASSERTED TO REPRODUCE `det.stop` EXACTLY
(rounded) — see `mismatched_baseline` in the report; a nonzero count there
means the replication is not faithful and every other number is suspect.

`risk_from_structure`'s REFUSAL behaviour is replicated too, not skipped: a
stop widened past `ign_max_risk_pct` is refused entirely under the live
`intraday_stop_cap_mode=refuse` default, never resized. Widening the stop can
therefore only ever hold a trade steady or turn it into a refusal, never make
a refused trade tradeable — so every variant is reported both by "R on trades
still taken" AND "how many of baseline's detections this variant would now
refuse", because that trade-off is the whole question this script answers.

VARIANTS
--------
baseline       lookback=live ign_stop_lookback_bars, buffer=live ign_stop_buffer_pct — unchanged.
wide_buffer    buffer widened to 0.40%, lookback unchanged.
long_lookback  lookback widened to 45 bars, buffer unchanged.
atr_floor_05   stop distance floored at 0.5x the day's ATR (whichever is wider).
atr_floor_10   stop distance floored at 1.0x the day's ATR (whichever is wider).

Diagnostic, matching `ladder_variant_check.py`'s own status: does not touch
`system_config` or `params/frozen.json`. Findings belong in `docs/FINDINGS.md`
as a proposal, per this project's "propose, never auto-apply" rule — nothing
here writes to the live system.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import cfg_float, cfg_int, get_supabase
from intraday import direction as D
from intraday.exit_policy import load_intraday_policy
from kite.kite_client import get_kite
from tools.replay.bars import BarSource
from tools.replay.contexts import bars_before
from tools.replay.detect import replay_symbol_day
from tools.replay.ladder_variant_check import _gross_r, _qty_for, _walk
from tools.replay.universe import build_universe_for_session, prev_day_reference


def _structural_stop(direction: str, window: list, lookback: int, buffer_pct: float,
                     entry: float, atr_pct: float | None,
                     atr_floor_mult: float | None) -> float:
    """Reproduces `ignition.py::_long`/`_short`'s stop arithmetic, parameterised."""
    w = window[-lookback:] if lookback else window
    if D.is_short(direction):
        swing = max(b.high for b in w)
        stop = swing * (1 + buffer_pct / 100.0)
        if atr_floor_mult and atr_pct:
            floor_dist = atr_floor_mult * (atr_pct / 100.0 * entry)
            stop = max(stop, entry + floor_dist)      # only ever widen
    else:
        swing = min(b.low for b in w)
        stop = swing * (1 - buffer_pct / 100.0)
        if atr_floor_mult and atr_pct:
            floor_dist = atr_floor_mult * (atr_pct / 100.0 * entry)
            stop = min(stop, entry - floor_dist)       # only ever widen
    return stop


@dataclass
class VariantResult:
    taken: list = field(default_factory=list)   # (r, terminal_action)
    refused: int = 0
    mismatched_baseline: int = 0


def run(start: str, end: str, universe_limit: int = 40) -> tuple[int, dict]:
    sb = get_supabase()
    src = BarSource(kite=get_kite())
    policy = load_intraday_policy(engine="IGN")

    live_lookback = cfg_int("ign_stop_lookback_bars", 20)
    live_buffer = cfg_float("ign_stop_buffer_pct", 0.12)
    max_risk = cfg_float("ign_max_risk_pct", 1.75)
    min_risk = cfg_float("intraday_min_risk_pct", 0.0)
    target_r = cfg_float("ign_target_r", 1.5)

    variants = {
        "baseline":      dict(lookback=live_lookback, buffer=live_buffer, atr_floor=None),
        "wide_buffer":   dict(lookback=live_lookback, buffer=0.40,        atr_floor=None),
        "long_lookback": dict(lookback=45,            buffer=live_buffer, atr_floor=None),
        "atr_floor_05":  dict(lookback=live_lookback, buffer=live_buffer, atr_floor=0.5),
        "atr_floor_10":  dict(lookback=live_lookback, buffer=live_buffer, atr_floor=1.0),
    }
    results = {name: VariantResult() for name in variants}
    baseline_n = 0

    d = _date.fromisoformat(start)
    d1 = _date.fromisoformat(end)
    while d <= d1:
        day = d.isoformat()
        if d.weekday() < 5:
            uni = build_universe_for_session(day, sb, limit=universe_limit)
            symbols = uni.symbols
            day_igns = 0
            if symbols:
                prev = prev_day_reference(day, symbols, sb)
                for sym in symbols:
                    day_bars = src.get(sym, day)
                    if not day_bars:
                        continue
                    dets = replay_symbol_day(sym, day, day_bars, prev=prev.get(sym))
                    atr_pct = float((prev.get(sym) or {}).get("atr_pct") or 0) or None

                    for det in dets:
                        if det.engine != "IGN":
                            continue
                        window = bars_before(day_bars, det.ts)
                        walk_bars = [b for b in day_bars if b.ts >= det.ts]
                        if len(walk_bars) < 2 or len(window) < 5:
                            continue
                        baseline_n += 1
                        day_igns += 1

                        for name, params in variants.items():
                            stop = round(_structural_stop(
                                det.direction, window, params["lookback"],
                                params["buffer"], det.entry, atr_pct,
                                params["atr_floor"]), 2)
                            risk = D.risk_per_share(det.entry, stop, det.direction)
                            risk_pct = (risk / det.entry * 100.0) if risk and det.entry else 0.0
                            if not risk or risk <= 0 or \
                               (min_risk > 0 and risk_pct < min_risk) or \
                               risk_pct > max_risk:
                                results[name].refused += 1
                                continue

                            if name == "baseline" and stop != round(det.stop, 2):
                                results[name].mismatched_baseline += 1

                            target = (det.entry + risk * target_r if not D.is_short(det.direction)
                                      else det.entry - risk * target_r)
                            pos = {
                                "entry_price": det.entry, "planned_stop": stop,
                                "planned_target": round(target, 2), "direction": det.direction,
                                "active_sl": stop, "high_water_mark": det.entry,
                                "current_qty": _qty_for(det.entry),
                            }
                            action, exit_price = _walk(pos, walk_bars, policy)
                            r = _gross_r(det.entry, exit_price, stop, det.direction)
                            results[name].taken.append((r, action))
            print(f"  {day}: {len(symbols)} universe symbols, {day_igns} IGN detections "
                  f"(cumulative {baseline_n}) — {src.coverage.line()}")
        d += timedelta(days=1)

    return baseline_n, results


def report(baseline_n: int, results: dict) -> None:
    print()
    print("=" * 78)
    print(f"IGN STOP VARIANT CHECK — {baseline_n} IGN detections, gross R (no costs)")
    print("=" * 78)
    if results["baseline"].mismatched_baseline:
        print(f"  ** WARNING: baseline stop mismatched det.stop on "
              f"{results['baseline'].mismatched_baseline} trades — reproduction is NOT "
              f"faithful, treat every number below with suspicion **")
    for name, res in results.items():
        rs = [r for r, _ in res.taken]
        n = len(rs)
        if n == 0:
            print(f"  {name:<15} n=0 taken, {res.refused} refused")
            continue
        mean = statistics.mean(rs)
        median = statistics.median(rs)
        se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
        wins = sum(1 for r in rs if r > 0)
        print(f"  {name:<15} n={n:<4} refused={res.refused:<4} "
              f"mean {mean:+.4f}R (SE {se:.4f})  median {median:+.4f}R  "
              f"win {wins}/{n}={wins/n:.1%}")
    print()
    print("  terminal-action distribution:")
    for name, res in results.items():
        actions = Counter(a for _, a in res.taken)
        print(f"    {name:<15} " + ", ".join(f"{a}={c}" for a, c in actions.most_common()))
    print()
    print(f"  n={baseline_n} baseline IGN detections over the window tested — diagnostic "
          f"only, same n<100 caveat ladder_variant_check.py applies to its own n=238.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-08-31")
    p.add_argument("--end", default="2026-09-10")
    p.add_argument("--universe-limit", type=int, default=40)
    args = p.parse_args()
    n, res = run(args.start, args.end, args.universe_limit)
    report(n, res)
