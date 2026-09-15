"""
SDN feature study — does the underlying name's own daily volatility, the
engine's own confidence, volume_ratio, or which of the three SDN conditions
fired separate winners from losers, replayed on real bars through SDN's own
live exit policy?

WHY THIS QUESTION, AND WHY NOW
--------------------------------
Both `tools.feature_edge_study` (dry-run, 15-Sep-2026) and a PENDING
brain_proposal from 23-Aug-2026 (`SDN/atr_pct_daily`, confidence 0.79) report
a split on the TAKEN-and-resolved population: atr_pct_daily <= 2.07 -> 0% win
(n=32) vs >= 3.27 -> 39% win (n=32). That reads as "higher ATR is better", on
n=32 each side — comfortably clearing this project's MIN_SEGMENT=15 bar, but
it is a label on `outcome_pct`, a field the live TAKEN gate already filtered
through (only cost-gate-passed rows have a resolved outcome at all). This
script is an INDEPENDENT check: real minute bars, SDN's OWN live
`ShortDistribution.evaluate()` re-run against them (not the recorded setup),
real `load_intraday_policy(engine="SDN")` exit walk — same shape as
`ign_entry_exit_variant_check.py` in this same directory.

INDEPENDENCE, ENFORCED BY tools/replay/independence.py
---------------------------------------------------------
This harness's own static-analysis check (`tools.verify --module
test_replay_harness`) forbids any script in this package from reading the
live detection record — because computing a feature split FROM the table the
split is meant to verify is circular. An earlier version of this script read
entry/stop/target/atr_pct_daily straight from that record and was correctly
caught by that check. This version detects from bars via `replay_symbol_day`
(which calls the real engine code) and reads `atr_pct_daily` from
`stock_data_daily` via `prev_day_reference` — the same independent source
`tools.replay.contexts.build_context` uses live — never from the live record.

    python -m tools.replay.sdn_atr_floor_variant_check --start 2026-08-01 --end 2026-09-15
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import get_supabase
from intraday import direction as D
from intraday.exit_policy import load_intraday_policy
from tools.replay.bars import BarSource
from tools.replay.detect import replay_symbol_day
from tools.replay.ladder_variant_check import _gross_r, _walk, _qty_for
from tools.replay.universe import build_universe_for_session, prev_day_reference


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
                    p = prev.get(sym) or {}
                    atr = float(p.get("atr_pct") or 0) or None

                    dets = replay_symbol_day(sym, day, day_bars, prev=p)
                    seen_keys = set()
                    for det in [x for x in dets if x.engine == "SDN"]:
                        # meta["sub_engine"] is the per-CONDITION label
                        # (VREJ/BRKD/TRP) since detect.py's own F-39-style fix
                        # (15-Sep-2026) — det.sub_engine (the dataclass field)
                        # stays s.strategy ("SDN" for all three, on purpose:
                        # that's what live dedup keys on, see detect.py).
                        cond = det.meta.get("sub_engine") or det.sub_engine
                        key = (det.symbol, cond)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)

                        walk_bars = [b for b in day_bars if b.ts >= det.ts]
                        if len(walk_bars) < 2 or atr is None:
                            continue

                        pos = {
                            "entry_price": det.entry, "planned_stop": det.stop,
                            "planned_target": det.target, "direction": det.direction,
                            "active_sl": det.stop, "high_water_mark": det.entry,
                            "current_qty": _qty_for(det.entry),
                        }
                        action, exit_price = _walk(pos, walk_bars, policy)
                        r_mult = _gross_r(det.entry, exit_price, det.stop, det.direction)
                        out.append({
                            "symbol": sym, "day": day, "sub_engine": cond,
                            "atr_pct_daily": atr, "confidence": det.confidence,
                            "volume_ratio": det.meta.get("volume_ratio"),
                            "r": r_mult, "action": action,
                        })
                print(f"  {day}: {len(symbols)} symbols -> {len(out)} cumulative SDN replays "
                      f"({src.coverage.line()})")
        d += timedelta(days=1)
    print(f"  {n_no_cache} symbol-days skipped, no cached bars")
    return out


def _report(title: str, buckets: dict[str, list[float]]) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)
    for name, rs in buckets.items():
        n = len(rs)
        if n == 0:
            print(f"  {name:<20} n=0")
            continue
        mean = statistics.mean(rs)
        median = statistics.median(rs)
        wins = sum(1 for x in rs if x > 0)
        se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
        print(f"  {name:<20} n={n:<4} mean {mean:+.3f}R (SE {se:.3f})  "
              f"median {median:+.3f}R  win {wins}/{n}={wins/n:.0%}")


def report(rows: list[dict]) -> None:
    atr_b = {"low (<=2.07)": [], "mid (2.07-3.27)": [], "high (>=3.27)": []}
    conf_b = {"low (<=0.66)": [], "mid (0.66-0.72)": [], "high (>=0.72)": []}
    vr_b = {"low (<=1.31)": [], "mid (1.31-1.52)": [], "high (>=1.52)": []}
    sub_b: dict[str, list[float]] = {}

    for r in rows:
        atr, conf, vr, rm = r["atr_pct_daily"], r["confidence"], r["volume_ratio"], r["r"]
        (atr_b["low (<=2.07)"] if atr <= 2.07 else
         atr_b["high (>=3.27)"] if atr >= 3.27 else
         atr_b["mid (2.07-3.27)"]).append(rm)
        (conf_b["low (<=0.66)"] if conf <= 0.66 else
         conf_b["high (>=0.72)"] if conf >= 0.72 else
         conf_b["mid (0.66-0.72)"]).append(rm)
        if vr is not None:
            vr = float(vr)
            (vr_b["low (<=1.31)"] if vr <= 1.31 else
             vr_b["high (>=1.52)"] if vr >= 1.52 else
             vr_b["mid (1.31-1.52)"]).append(rm)
        sub_b.setdefault(r["sub_engine"], []).append(rm)

    print(f"\n  {len(rows)} total independently-redetected SDN trade-replays")
    _report("SDN atr_pct_daily (from stock_data_daily, independently re-detected)", atr_b)
    _report("SDN confidence (from re-detection)", conf_b)
    _report("SDN volume_ratio (from re-detection)", vr_b)
    _report("SDN by sub_engine", sub_b)
    print(f"\n  sufficiency bar in this repo is 40-100/cell; read cells against that.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--universe-limit", type=int, default=60)
    args = ap.parse_args()
    rows = run(args.start, args.end, args.universe_limit)
    report(rows)
