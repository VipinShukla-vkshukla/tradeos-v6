"""
SDN/BRKD volume_ratio floor — does raising the opening-range-breakdown
condition's own volume confirmation gate (`intraday_short_orb_min_vol_ratio`,
today 1.1) cut its worst detections without cutting its good ones?

WHY THIS QUESTION, AND WHY NOW
--------------------------------
Three independent measurements in this session's own prior two entries
(the real closed book, a family-level replay, and a corrected
per-condition replay) all agree: BRKD is SDN's weakest condition (real
trades: mean -0.010R/n=9; corrected replay: mean -0.201R/n=56 — the only
sub-engine with a negative mean in every measurement). The same session's
`sdn_atr_floor_variant_check.py` found SDN's volume_ratio relationship
non-monotonic pool-wide, but that mixed VREJ/TRP/BRKD together — BRKD is the
only condition that GATES on volume_ratio at all today (VREJ and TRP only use
it as a confidence boost, never a hard floor), so this is the one condition
where tightening this specific number is actually a lever that exists.

METHOD — reuses the FLOOR-GATE EQUIVALENCE, not a re-detect per variant
---------------------------------------------------------------------------
`_range_breakdown`'s gate is `if not vr or vr < floor: return None` — a pure
MINIMUM. Every detection that would survive a HIGHER floor is, by
construction, already inside the population detected at today's floor (1.1);
raising the floor only ever REMOVES detections, never changes the entry/
stop/target of one that survives. So this replays ONCE at today's live floor
(genuine re-detection via `replay_symbol_day`, independent of the live
detection record — same contract as this directory's other SDN checks) and
buckets the results by whether each detection's own `volume_ratio` (stamped
by the engine itself, in `Setup.meta`, from real bars) would have cleared a
higher candidate floor. This is mathematically identical to re-detecting at
each floor and cheaper; `ign_entry_exit_variant_check.py`'s approach (re-run
detection per variant) is necessary there because a looser trigger can find
NEW setups a stricter one never would — the reverse direction, which has no
such equivalence. Here the direction is tightening, which does.

    python -m tools.replay.sdn_brkd_volume_floor_variant_check --start 2026-08-01 --end 2026-09-15
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import get_supabase
from intraday.exit_policy import load_intraday_policy
from tools.replay.bars import BarSource
from tools.replay.detect import replay_symbol_day
from tools.replay.ladder_variant_check import _gross_r, _walk, _qty_for
from tools.replay.universe import build_universe_for_session, prev_day_reference

FLOORS = [1.1, 1.3, 1.5, 1.8]   # 1.1 = today's live floor, baseline


def run(start: str, end: str, universe_limit: int = 60) -> list[dict]:
    sb = get_supabase()
    src = BarSource(kite=None)
    policy = load_intraday_policy(engine="SDN")

    out: list[dict] = []
    d, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    n_no_cache = 0
    while d <= d1:
        day = d.isoformat()
        if d.weekday() < 5:
            uni = build_universe_for_session(day, sb, limit=universe_limit)
            symbols = uni.symbols
            if symbols:
                prev = prev_day_reference(day, symbols, sb)
                for sym in symbols:
                    day_bars = src.get(sym, day)
                    if not day_bars:
                        n_no_cache += 1
                        continue

                    dets = replay_symbol_day(sym, day, day_bars, prev=prev.get(sym))
                    seen = set()
                    for det in dets:
                        if det.engine != "SDN":
                            continue
                        cond = det.meta.get("sub_engine") or det.sub_engine
                        if cond != "BRKD":
                            continue
                        if sym in seen:
                            continue
                        seen.add(sym)

                        vr = det.meta.get("volume_ratio")
                        if vr is None:
                            continue
                        vr = float(vr)

                        walk_bars = [b for b in day_bars if b.ts >= det.ts]
                        if len(walk_bars) < 2:
                            continue

                        pos = {
                            "entry_price": det.entry, "planned_stop": det.stop,
                            "planned_target": det.target, "direction": det.direction,
                            "active_sl": det.stop, "high_water_mark": det.entry,
                            "current_qty": _qty_for(det.entry),
                        }
                        action, exit_price = _walk(pos, walk_bars, policy)
                        r_mult = _gross_r(det.entry, exit_price, det.stop, det.direction)
                        out.append({"symbol": sym, "day": day, "volume_ratio": vr, "r": r_mult})
                print(f"  {day}: {len(symbols)} symbols -> {len(out)} cumulative BRKD replays "
                      f"({src.coverage.line()})")
        d += timedelta(days=1)
    print(f"  {n_no_cache} symbol-days skipped, no cached bars")
    return out


def report(rows: list[dict]) -> None:
    print(f"\n  {len(rows)} distinct BRKD detections at today's live floor (1.1)")
    print("=" * 80)
    print("SDN/BRKD volume_ratio FLOOR — raising the gate, cumulative")
    print("=" * 80)
    for floor in FLOORS:
        rs = [r["r"] for r in rows if r["volume_ratio"] >= floor]
        n = len(rs)
        if n == 0:
            print(f"  floor >= {floor:<4} n=0")
            continue
        mean = statistics.mean(rs)
        median = statistics.median(rs)
        wins = sum(1 for x in rs if x > 0)
        se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
        pct_removed = 100.0 * (1 - n / len(rows))
        print(f"  floor >= {floor:<4} n={n:<4} ({pct_removed:4.1f}% of baseline removed)  "
              f"mean {mean:+.3f}R (SE {se:.3f})  median {median:+.3f}R  win {wins}/{n}={wins/n:.0%}")

    print(f"\n  sufficiency bar in this repo is 40-100/cell; read cells against that.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--universe-limit", type=int, default=60)
    args = ap.parse_args()
    rows = run(args.start, args.end, args.universe_limit)
    report(rows)
