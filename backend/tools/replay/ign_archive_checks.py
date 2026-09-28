"""
Checks on the archive-built IGN study (ign_archive_events.py). Both read TRAIN data only; nothing here opens the
sealed holdout table.

    python -m tools.replay.ign_archive_checks parity   # vectorised scanner == tested first_trigger, day by day
    python -m tools.replay.ign_archive_checks stress   # the spec's candidate: by year, cost, circuit band, slot cap

`parity` is the guard on the one piece of new detection code: for 40 random symbols it walks every training day
through the tested reference (ign_event_features.first_trigger) and demands the scanner's (day, bar, cell) set be
identical. `stress` asks the questions a paper edge has to survive before it is worth trading: does it hold every
year, how much extra slippage does it take to erase it, and how much of it depends on trades that finished the day
pinned at an exchange circuit price (where the exit the simulator books could not have happened).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.kite_history import store as KH
from tools.replay import ign_archive_events as A
from tools.replay import ign_event_features as F
from tools.replay import ign_event_study as E
from tools.replay.ign_event_table import MAX_GAP_PCT, MIN_IDX, MIN_SESSION_BARS, REASON_CODE, cells, load_train
from tools.replay.ign_feature_stats import MIN_PRICE, MIN_TURNOVER_CR


def reference_triggers(mdf: pd.DataFrame, daily: list, atr_prior: np.ndarray, eligible: set, until=None) -> set:
    """{(day, bar, cell)} the slow way: one day at a time through the tested `first_trigger`, with the same
    eligibility, minimum-bars and opening-gap rules the scanner applies. The definition the scanner must equal."""
    pos = {b.date: p for p, b in enumerate(daily)}
    out = set()
    for day, g in mdf.assign(date=mdf["ts"].dt.date).groupby("date", sort=False):
        p = pos.get(day)
        if (until is not None and day > until) or day not in eligible or p is None or p < 1 or len(g) < MIN_SESSION_BARS:
            continue
        d = F.DayBars(g["open"].to_numpy(float), g["high"].to_numpy(float), g["low"].to_numpy(float),
                      g["close"].to_numpy(float), g["volume"].to_numpy(float))
        pc, pv = daily[p - 1].close, daily[p - 1].volume
        if abs((d.c[0] / pc - 1) * 100) > MAX_GAP_PCT:
            continue
        for name, t, v in cells():
            i = F.first_trigger(d, pc, pv, t, v, min_idx=MIN_IDX)
            if i is not None:
                out.add((day, int(i), name))
        if np.isfinite(atr_prior[p]):
            i = F.first_trigger(d, pc, pv, max(3.5, 1.2 * float(atr_prior[p])), 2.0, min_idx=MIN_IDX, require_feasible=True)
            if i is not None:
                out.add((day, int(i), "LIVE"))
    return out


def parity(n_symbols: int = 40, seed: int = 11) -> bool:
    root = KH.DEFAULT_ROOT
    syms = KH.list_symbols(root, "minute", "NSE")
    pick = list(np.random.default_rng(seed).choice(syms, n_symbols, replace=False))
    train_end = pd.Timestamp(A.TRAIN_END).date()
    tot_ref = tot_scan = tot_same = 0
    bad = []
    t0 = time.time()
    for sym in pick:
        mdf = KH.read(root, "minute", "NSE", sym, start=A.STUDY_START)
        ddf = KH.read(root, "day", "NSE", sym, start="2014-01-01")
        if mdf.empty or ddf.empty:
            continue
        daily = A._daily_bars(ddf)
        if len(daily) < 30:
            continue
        atr_prior = A._atr_pct_prior(daily)
        eligible = {daily[p].date for p in range(1, len(daily))
                    if daily[p - 1].close >= MIN_PRICE and daily[p - 1].close * daily[p - 1].volume / 1e7 >= MIN_TURNOVER_CR}
        got = A.scan_triggers(mdf, daily, atr_prior, eligible)
        fg = {(d, i, c) for d, by in got.items() if d <= train_end for i, cs in by.items() for c in cs}
        fr = reference_triggers(mdf, daily, atr_prior, eligible, train_end)
        tot_ref, tot_scan, tot_same = tot_ref + len(fr), tot_scan + len(fg), tot_same + len(fr & fg)
        if fr != fg:
            bad.append((sym, sorted(fr - fg)[:3], sorted(fg - fr)[:3]))
    ok = tot_ref == tot_scan == tot_same and tot_ref > 0
    print(f"{n_symbols} symbols, train days only, {time.time() - t0:.0f}s: reference {tot_ref}  scanner {tot_scan}  identical {tot_same}  ->  "
          f"{'EXACT MATCH' if ok else 'MISMATCH'}")
    for b in bad[:8]:
        print("  ", b)
    return ok


def stress() -> None:
    df, res, names = load_train()
    assert df["day"].max() <= A.TRAIN_END
    spec = json.loads((Path(__file__).parent / "ign_event_spec.json").read_text(encoding="utf-8"))
    if spec.get("decision") != "candidate":
        print(f"spec decision is '{spec.get('decision')}': there is no candidate to stress")
        return
    j = names.index(spec["policy"])
    flt = [E.Filter(f["feature"], f["op"], f["thr"]) for f in spec["filters"]]
    sc = E._struct_cols(names)

    def net(cost):
        return E.net_r(res["gross"], res["risk"], cost, E.RISK_BAND, sc)[:, j]

    base = net(E.COST_PCT)
    m = (df["cell"].to_numpy() == spec["cell"]) & E.apply_filters(df, flt) & ~np.isnan(base)
    d = df[m].copy()
    d["r"] = base[m]
    d["gross"] = res["gross"][:, j][m].astype(float)
    d["reason"] = res["reason"][:, j][m]

    def line(label, x):
        x = x[x["r"].notna()]
        if not len(x):
            return f"  {label:<34} n=0"
        mean = x["r"].mean()
        se = np.sqrt((((x["r"] - mean).groupby(x["day"]).sum()) ** 2).sum()) / len(x)
        return f"  {label:<34} n={len(x):>5}  net R {mean:+.3f} (se {se:.3f})  win {(x['r'] > 0).mean() * 100:4.1f}%"

    print(f"candidate {spec['cell']} {spec['policy']} {[(f.feature, f.op, round(f.thr, 2)) for f in flt]}")
    print(line("ALL TRAIN", d))
    print("\nBY YEAR")
    for y, g in d.groupby(d["day"].str[:4]):
        print(line(y, g))
    print("\nEXTRA ROUND-TRIP COST on top of 0.2063%")
    for extra in (0.0, 0.05, 0.10, 0.20, 0.30):
        r = net(E.COST_PCT + extra)[m]
        r = r[~np.isnan(r)]
        print(f"  +{extra:.2f}%   net R {r.mean():+.3f}")
    print("\nSLOT CAP (first K candidates each day)")
    d2 = d.sort_values(["day", "idx"])
    for k in (1, 2, 4):
        print(line(f"K={k}", d2.groupby("day").head(k)))
    d["exit_vs_pc"] = (d["fo_entry"] * (1 - d["gross"] / 100) / d["prev_close"] - 1) * 100
    eod = d["reason"] == REASON_CODE["EOD"]
    pinned = eod & pd.concat([(d["exit_vs_pc"] - b).abs() <= 0.15 for b in (5, 10, 20)], axis=1).any(axis=1)
    print(f"\nFINISHED THE DAY PINNED AT A CIRCUIT PRICE (+5/+10/+20% of prior close): {int(pinned.sum())} trades = {pinned.mean() * 100:.1f}%, "
          f"booked at {d.loc[pinned, 'r'].mean():+.3f}R")
    for pen in (None, -1.138, -3.0):
        r2 = d["r"].copy()
        if pen is not None:
            r2[pinned] = pen
        print(f"  {'as booked' if pen is None else f'if each had really lost {pen:+.2f}R'}: net R {r2.mean():+.3f}")
    print(f"  {100 * ((d['fo_entry'] * 1.015 / d['prev_close'] - 1) * 100 > 5).mean():.0f}% of stops sit above +5% of prior close: "
          f"in a 5%-band stock such a stop can never trigger")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("check", choices=("parity", "stress"))
    args = p.parse_args()
    if args.check == "parity":
        return 0 if parity() else 1
    stress()
    return 0


if __name__ == "__main__":
    sys.exit(main())
