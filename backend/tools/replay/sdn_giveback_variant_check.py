"""
SDN-scoped giveback — does loosening the profit-lock, FOR SDN ONLY, capture
more of the R:R its own entries are gated on requiring (min_rr=1.3), without
giving back so much that a real reversal erases the gain?

WHY THIS QUESTION
------------------
`sdn_atr_floor_variant_check.py` (15-Sep-2026) found SDN's real
`GAVE_BACK_THE_MOVE` winners keep ~69% of their own peak favourable move on
average (peak +0.59R -> realized +0.41R) — the pooled `intraday_giveback_pct`
(30%, `intraday_giveback_min_r` 0.5) locking in well short of the 1.3R the
entry side requires before a trade is even taken. That pooled number was
calibrated on 27 trades book-wide in mid-August, before SDN was the book's
dominant engine (docs/FINDINGS.md, 11-Aug-2026). `load_intraday_policy(engine=
"SDN")` already supports a scoped override (`sdn_giveback_pct`); nothing has
ever set it. This is the direct, evidence-motivated replay test — not a
change, a measurement of what a change would do.

VARIANTS
--------
giveback_pct: 30 (today's pooled value, baseline) / 45 / 65 (matches the
magnitude `ign_entry_exit_variant_check.py`'s "long_exit" already tested for
IGN, kept for cross-tool comparability). `giveback_min_r` held at the pooled
0.5 in every variant — this is a single-lever test, not a 2x2, because the
sufficiency bar (40-100/cell) is already the binding constraint on n here.

METHOD — same independence contract as sdn_atr_floor_variant_check.py
------------------------------------------------------------------------
Real minute bars (local cache only), SDN's own `ShortDistribution.evaluate()`
re-run via `replay_symbol_day` (genuine re-detection, not the recorded
setup), `_cfg_override` to move only `sdn_giveback_pct` for the duration of
each variant's walk (every other live switch reads exactly as production).

    python -m tools.replay.sdn_giveback_variant_check --start 2026-08-01 --end 2026-09-15
"""

from __future__ import annotations

import argparse
import statistics
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config as _cfgmod
from config import get_supabase
from intraday.exit_policy import load_intraday_policy
from tools.replay.bars import BarSource
from tools.replay.detect import replay_symbol_day
from tools.replay.ladder_variant_check import _gross_r, _walk, _qty_for
from tools.replay.universe import build_universe_for_session, prev_day_reference


@contextmanager
def _cfg_override(overrides: dict):
    """Merge onto the real live config snapshot; restore in finally.

    Same contract as ign_entry_exit_variant_check.py's own helper — never
    blanks keys not under test, unlike tests.cfg_ctx.
    """
    _cfgmod.cfg("_force_load", "")
    saved = dict(_cfgmod._sys_config or {})
    merged = dict(saved)
    merged.update({k: str(v) for k, v in overrides.items()})
    _cfgmod._sys_config = merged
    try:
        yield
    finally:
        _cfgmod._sys_config = saved


GIVEBACK_VARIANTS = {
    "baseline_30": 30.0,
    "loosen_45":   45.0,
    "loosen_65":   65.0,
}


@dataclass
class Trade:
    symbol: str
    day: str
    sub_engine: str
    entry: float
    r: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)


def run(start: str, end: str, universe_limit: int = 60) -> list[Trade]:
    sb = get_supabase()
    src = BarSource(kite=None)

    trades: list[Trade] = []
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
                    seen_keys = set()
                    for det in [x for x in dets if x.engine == "SDN"]:
                        cond = det.meta.get("sub_engine") or det.sub_engine
                        key = (det.symbol, cond)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)

                        walk_bars = [b for b in day_bars if b.ts >= det.ts]
                        if len(walk_bars) < 2:
                            continue

                        trow = Trade(symbol=sym, day=day, sub_engine=cond, entry=det.entry)
                        for vname, pct in GIVEBACK_VARIANTS.items():
                            with _cfg_override({"sdn_giveback_pct": pct}):
                                policy = load_intraday_policy(engine="SDN")
                            pos = {
                                "entry_price": det.entry, "planned_stop": det.stop,
                                "planned_target": det.target, "direction": det.direction,
                                "active_sl": det.stop, "high_water_mark": det.entry,
                                "current_qty": _qty_for(det.entry),
                            }
                            action, exit_price = _walk(pos, walk_bars, policy)
                            trow.r[vname] = _gross_r(det.entry, exit_price, det.stop, det.direction)
                            trow.action[vname] = action
                        trades.append(trow)
                print(f"  {day}: {len(symbols)} symbols -> {len(trades)} cumulative SDN trade-rows "
                      f"({src.coverage.line()})")
        d += timedelta(days=1)
    print(f"  {n_no_cache} symbol-days skipped, no cached bars")
    return trades


def report(trades: list[Trade]) -> None:
    print(f"\n  {len(trades)} distinct SDN setups, replayed across {len(GIVEBACK_VARIANTS)} giveback variants")
    print("=" * 80)
    print("SDN giveback_pct VARIANT CHECK — whole population")
    print("=" * 80)
    for vname in GIVEBACK_VARIANTS:
        rs = [t.r[vname] for t in trades if vname in t.r]
        n = len(rs)
        if n == 0:
            print(f"  {vname:<16} n=0"); continue
        mean = statistics.mean(rs)
        median = statistics.median(rs)
        wins = sum(1 for x in rs if x > 0)
        se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
        worst = min(rs)
        print(f"  {vname:<16} n={n:<4} mean {mean:+.3f}R (SE {se:.3f})  median {median:+.3f}R  "
              f"win {wins}/{n}={wins/n:.0%}  worst {worst:+.3f}R")

    print()
    for sub in ("VREJ", "BRKD", "TRP"):
        rows = [t for t in trades if t.sub_engine == sub]
        if not rows:
            continue
        print(f"  --- {sub} only (n={len(rows)}) ---")
        for vname in GIVEBACK_VARIANTS:
            rs = [t.r[vname] for t in rows if vname in t.r]
            n = len(rs)
            if n == 0:
                print(f"    {vname:<16} n=0"); continue
            mean = statistics.mean(rs)
            median = statistics.median(rs)
            wins = sum(1 for x in rs if x > 0)
            print(f"    {vname:<16} n={n:<4} mean {mean:+.3f}R  median {median:+.3f}R  "
                  f"win {wins}/{n}={wins/n:.0%}")
        print()

    print(f"  sufficiency bar in this repo is 40-100/cell; read the whole-population "
          f"rows against that, the per-sub_engine rows as directional only.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--universe-limit", type=int, default=60)
    args = ap.parse_args()
    ts = run(args.start, args.end, args.universe_limit)
    report(ts)
