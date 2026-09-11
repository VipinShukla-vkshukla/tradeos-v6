"""
IGN entry/exit variant check — two independent levers, tested separately and
combined: does entering EARLIER (looser trigger) or staying in LONGER
(looser giveback + a bigger target) actually improve outcomes, on the real
replayed population, not just the PINELABS/HINDALCO anecdotes that motivated
the question?

WHY BOTH LEVERS, AND WHY SEPARATELY
------------------------------------
"Enter earlier" changes WHICH detections exist at all — a looser
`ign_min_pct`/`ign_min_atr_frac` fires on setups the live engine never saw.
"Stay longer" changes what happens to a detection ONCE taken — a looser
`giveback_pct` and a bigger `target_r`. These are orthogonal: entering
earlier into a trade that still exits early gains nothing, and staying in
longer on the SAME (late) entries is a different question from whether
earlier entries exist to stay in. Tested as a 2x2 so each lever's own effect
is visible, not just the combination.

WHAT IS REUSED
--------------
`replay_symbol_day` (detect.py), `_walk`/`_gross_r`/`_qty_for`
(ladder_variant_check.py) — unmodified, same as every other replay tool in
this directory. `risk_from_structure`'s own refusal behaviour is NOT
reproduced here the way ign_stop_variant_check.py did — the entry-side
lever changes the TRIGGER, not the stop, so `ignition.py`'s own risk-cap
logic runs unchanged inside `replay_symbol_day` -> `evaluate_one` ->
`eng.evaluate()` for every variant, live code, not a copy.

HOW THE ENTRY LEVER IS APPLIED WITHOUT TOUCHING LIVE system_config
---------------------------------------------------------------------
`ignition.py` reads `ign_min_pct`/`ign_min_atr_frac`/`ign_min_volume_ratio`
via `cfg_float()`, which reads the process-wide `config._sys_config` cache.
`tests/cfg_ctx()` exists for exactly this but REPLACES the whole cache,
which would silently revert every OTHER live switch (`ign_target_r`,
`ign_max_risk_pct`, `intraday_giveback_pct`, ...) to each call site's
hardcoded default for the duration — a real risk for a script whose result
depends on those other values staying at their true live settings.
`_cfg_override()` below instead MERGES onto a snapshot of the real cache
and restores it in `finally`, so only the keys under test move.

VARIANTS
--------
entry x exit, 2x2:
  base_entry / base_exit    — today's live IGN, unmodified.
  early_entry / base_exit   — ign_min_pct 3.5->2.2, ign_min_atr_frac 1.2->0.85.
  base_entry / long_exit    — giveback_pct 30->65, target_r 1.5->3.5.
  early_entry / long_exit   — both together.

Diagnostic, matching every other tool in this directory: does not touch
`system_config`. Findings belong in `docs/FINDINGS.md` as a proposal.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config as _cfgmod
from config import cfg_float, get_supabase
from intraday import direction as D
from intraday.exit_policy import load_intraday_policy
from kite.kite_client import get_kite
from tools.replay.bars import BarSource
from tools.replay.detect import replay_symbol_day
from tools.replay.ladder_variant_check import _gross_r, _qty_for, _walk
from tools.replay.universe import build_universe_for_session, prev_day_reference


@contextmanager
def _cfg_override(overrides: dict):
    """Merge onto the real live config snapshot; restore in finally.

    Unlike tests.cfg_ctx, this NEVER blanks out keys not under test — every
    other live system_config value keeps reading exactly as it does in
    production for the duration of the `with` block.
    """
    _cfgmod.cfg("_force_load", "")  # touch cfg() once so _sys_config is populated
    saved = dict(_cfgmod._sys_config or {})
    merged = dict(saved)
    merged.update({k: str(v) for k, v in overrides.items()})
    _cfgmod._sys_config = merged
    try:
        yield
    finally:
        _cfgmod._sys_config = saved


ENTRY_VARIANTS = {
    "base_entry":  {},
    "early_entry": {"ign_min_pct": 2.2, "ign_min_atr_frac": 0.85},
}
EXIT_VARIANTS = {
    "base_exit": {},
    "long_exit": {"giveback_pct": 65.0},
}
EXIT_TARGET_R = {"base_exit": None, "long_exit": 3.5}   # None = keep detection's own target


@dataclass
class Trade:
    symbol: str
    day: str
    direction: str
    entry: float
    r: dict = field(default_factory=dict)          # variant_key -> gross R
    action: dict = field(default_factory=dict)      # variant_key -> terminal action


def run(start: str, end: str, universe_limit: int = 40) -> list[Trade]:
    sb = get_supabase()
    src = BarSource(kite=get_kite())
    base_policy = load_intraday_policy(engine="IGN")

    trades: list[Trade] = []
    d, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    while d <= d1:
        day = d.isoformat()
        if d.weekday() < 5:
            uni = build_universe_for_session(day, sb, limit=universe_limit)
            symbols = uni.symbols
            if symbols:
                prev = prev_day_reference(day, symbols, sb)
                day_n = 0
                for sym in symbols:
                    day_bars = src.get(sym, day)
                    if not day_bars:
                        continue

                    # One detection pass PER ENTRY VARIANT — a looser trigger
                    # finds setups the live engine's own thresholds never
                    # produced, not just an earlier copy of the same one.
                    dets_by_entry_variant = {}
                    for ev_name, ev_overrides in ENTRY_VARIANTS.items():
                        with _cfg_override(ev_overrides):
                            dets = replay_symbol_day(sym, day, day_bars, prev=prev.get(sym))
                        dets_by_entry_variant[ev_name] = [x for x in dets if x.engine == "IGN"]

                    # Key on (direction, dedup-rounded entry) so the SAME
                    # underlying trade found by both entry variants is
                    # walked once per exit variant, not double-counted —
                    # early_entry's list is a SUPERSET in time (fires
                    # sooner) so its first detection per key is kept when
                    # both variants agree a trade exists at all.
                    base_keys = {(x.symbol, x.direction) for x in dets_by_entry_variant["base_entry"]}

                    for ev_name, dets in dets_by_entry_variant.items():
                        seen_keys = set()
                        for det in dets:
                            key = (det.symbol, det.direction)
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            walk_bars = [b for b in day_bars if b.ts >= det.ts]
                            if len(walk_bars) < 2:
                                continue

                            # One Trade row per (entry_variant, symbol, day, direction).
                            trow = Trade(symbol=sym, day=day, direction=det.direction, entry=det.entry)

                            risk = D.risk_per_share(det.entry, det.stop, det.direction) or det.entry * 0.005
                            for xv_name, xv_pol_overrides in EXIT_VARIANTS.items():
                                policy = dict(base_policy)
                                policy.update(xv_pol_overrides)
                                target_r = EXIT_TARGET_R[xv_name]
                                if target_r is not None:
                                    target = (det.entry + risk * target_r if not D.is_short(det.direction)
                                              else det.entry - risk * target_r)
                                else:
                                    target = det.target
                                pos = {
                                    "entry_price": det.entry, "planned_stop": det.stop,
                                    "planned_target": target, "direction": det.direction,
                                    "active_sl": det.stop, "high_water_mark": det.entry,
                                    "current_qty": _qty_for(det.entry),
                                }
                                action, exit_price = _walk(pos, walk_bars, policy)
                                r = _gross_r(det.entry, exit_price, det.stop, det.direction)
                                vk = f"{ev_name}|{xv_name}"
                                trow.r[vk] = r
                                trow.action[vk] = action
                            trow.r[f"{ev_name}/entry_tag"] = True
                            trades.append(trow)
                            day_n += 1
                print(f"  {day}: {len(symbols)} symbols, {day_n} entry-variant trade-rows "
                      f"(cumulative {len(trades)}) — {src.coverage.line()}")
        d += timedelta(days=1)
    return trades


def report(trades: list[Trade]) -> None:
    print()
    print("=" * 90)
    print(f"IGN ENTRY/EXIT VARIANT CHECK — {len(trades)} trade-rows across both entry variants")
    print("=" * 90)
    by_variant: dict[str, list[float]] = defaultdict(list)
    by_variant_action: dict[str, Counter] = defaultdict(Counter)
    for t in trades:
        for ev in ENTRY_VARIANTS:
            for xv in EXIT_VARIANTS:
                vk = f"{ev}|{xv}"
                if vk in t.r:
                    by_variant[vk].append(t.r[vk])
                    by_variant_action[vk][t.action[vk]] += 1

    for ev in ENTRY_VARIANTS:
        for xv in EXIT_VARIANTS:
            vk = f"{ev}|{xv}"
            rs = by_variant.get(vk, [])
            n = len(rs)
            if n == 0:
                print(f"  {vk:<28} n=0"); continue
            mean = statistics.mean(rs)
            median = statistics.median(rs)
            se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
            wins = sum(1 for r in rs if r > 0)
            print(f"  {vk:<28} n={n:<4} mean {mean:+.4f}R (SE {se:.4f})  "
                  f"median {median:+.4f}R  win {wins}/{n}={wins/n:.1%}")
    print()
    for vk, actions in by_variant_action.items():
        print(f"  {vk:<28} " + ", ".join(f"{a}={c}" for a, c in actions.most_common()))

    print()
    print("  --- same-stock spotlight: PINELABS / PWL / PAYTM / COCHINSHIP ---")
    for sym in ["PINELABS", "PWL", "PAYTM", "COCHINSHIP"]:
        rows = [t for t in trades if t.symbol == sym]
        if not rows:
            print(f"  {sym}: no replayed detection in this window"); continue
        for t in rows:
            cells = "  ".join(f"{ev}|{xv}={t.r.get(f'{ev}|{xv}', float('nan')):+.3f}R"
                               for ev in ENTRY_VARIANTS for xv in EXIT_VARIANTS
                               if f"{ev}|{xv}" in t.r)
            print(f"  {sym:<12} {t.day} entry={t.entry:<9} {cells}")

    n = len(trades)
    print(f"\n  n={n} — diagnostic only, same n<100-per-cell caveat every other replay "
          f"tool in this directory carries.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-09-01")
    p.add_argument("--end", default="2026-09-11")
    p.add_argument("--universe-limit", type=int, default=40)
    args = p.parse_args()
    ts = run(args.start, args.end, args.universe_limit)
    report(ts)
