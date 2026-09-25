"""
IGN target R:R replay -- tests whether IGN's real 1.5R target (hardcoded
default in ignition.py, never overridden) is leaving money on the table,
against IGN's own real 47 detections since launch (docs/FINDINGS.md,
23-Sep-2026). Real minute bars, the real evaluate_intraday_exit() ladder,
real entry/stop -- only the target R:R is varied.
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_supabase
from intraday import direction as D
from intraday.exit_policy import evaluate_intraday_exit, last_completed_close, load_intraday_policy
from kite.kite_client import get_kite
from tools.replay.bars import BarSource
from tools.replay.ladder import _fill_price

IGN_DETECTIONS = [
    ("2026-09-09","ADANIENT","2026-09-09 04:46:18.939072+00","LONG",3081.4,3039.55),
    ("2026-09-09","ADANIPORTS","2026-09-09 07:56:02.365136+00","LONG",1775.9,1764.88),
    ("2026-09-09","CHENNPETRO","2026-09-09 04:14:07.914789+00","LONG",1524.5,1498.4),
    ("2026-09-09","COFORGE","2026-09-09 04:18:02.72394+00","SHORT",1835.0,1856.73),
    ("2026-09-09","HBLENGINE","2026-09-09 04:54:15.054611+00","LONG",760.85,752.9),
    ("2026-09-09","INFY","2026-09-09 04:26:16.518576+00","SHORT",1042.9,1055.06),
    ("2026-09-09","MAXHEALTH","2026-09-09 04:43:43.780554+00","LONG",1033.9,1021.27),
    ("2026-09-09","PAYTM","2026-09-09 04:19:03.02087+00","LONG",1736.0,1707.95),
    ("2026-09-10","REDINGTON","2026-09-10 06:33:02.973109+00","LONG",398.05,393.78),
    ("2026-09-10","TEGA","2026-09-10 04:21:14.990673+00","LONG",1772.9,1742.51),
    ("2026-09-11","COCHINSHIP","2026-09-11 04:10:27.663215+00","SHORT",1434.5,1459.25),
    ("2026-09-11","DLF","2026-09-11 04:11:34.554329+00","SHORT",632.5,639.42),
    ("2026-09-11","GODREJPROP","2026-09-11 04:11:03.915929+00","SHORT",1769.9,1796.65),
    ("2026-09-11","HINDALCO","2026-09-11 04:02:31.52051+00","SHORT",978.2,991.19),
    ("2026-09-11","INDUSTOWER","2026-09-11 04:56:15.801994+00","LONG",385.05,380.54),
    ("2026-09-11","LICHSGFIN","2026-09-11 07:30:17.227764+00","LONG",571.75,562.32),
    ("2026-09-11","PAYTM","2026-09-11 06:49:17.380644+00","LONG",1805.9,1795.04),
    ("2026-09-11","PINELABS","2026-09-11 04:56:18.591442+00","LONG",185.99,182.87),
    ("2026-09-11","PWL","2026-09-11 06:46:20.133692+00","LONG",136.17,134.56),
    ("2026-09-15","AFCONS","2026-09-15 04:07:36.11546+00","LONG",294.6,289.9),
    ("2026-09-15","DATAPATTNS","2026-09-15 04:18:01.201638+00","SHORT",4609.8,4685.62),
    ("2026-09-15","HCLTECH","2026-09-15 04:06:24.395971+00","LONG",1285.0,1262.98),
    ("2026-09-15","INFY","2026-09-15 04:02:33.222164+00","LONG",1090.4,1073.71),
    ("2026-09-15","PERSISTENT","2026-09-15 04:00:14.310783+00","LONG",5744.5,5647.22),
    ("2026-09-15","TECHM","2026-09-15 04:06:02.225121+00","LONG",1625.0,1608.87),
    ("2026-09-15","TMPV","2026-09-15 04:24:31.957855+00","LONG",315.5,313.42),
    ("2026-09-16","GROWW","2026-09-16 04:35:56.796528+00","SHORT",188.95,190.23),
    ("2026-09-16","PATANJALI","2026-09-16 05:20:04.315263+00","LONG",352.25,347.33),
    ("2026-09-16","PREMIERENE","2026-09-16 06:26:26.578932+00","SHORT",903.9,914.3),
    ("2026-09-16","SYRMA","2026-09-16 04:33:00.583554+00","SHORT",1426.4,1450.74),
    ("2026-09-17","HBLENGINE","2026-09-17 06:10:00.895974+00","LONG",749.3,740.46),
    ("2026-09-17","POLICYBZR","2026-09-17 05:03:28.106822+00","SHORT",1745.2,1758.01),
    ("2026-09-17","SYRMA","2026-09-17 04:30:13.215617+00","LONG",1645.0,1620.95),
    ("2026-09-17","TEGA","2026-09-17 06:20:24.16936+00","LONG",1829.7,1809.53),
    ("2026-09-18","JYOTICNC","2026-09-18 06:20:02.414529+00","LONG",1041.15,1029.01),
    ("2026-09-21","CARBORUNIV","2026-09-21 04:38:03.244256+00","LONG",1214.6,1204.05),
    ("2026-09-21","COHANCE","2026-09-21 07:56:53.068091+00","LONG",468.85,462.44),
    ("2026-09-21","EMMVEE","2026-09-21 04:30:03.759654+00","LONG",349.0,344.64),
    ("2026-09-21","ENGINERSIN","2026-09-21 05:28:26.559654+00","LONG",279.55,274.67),
    ("2026-09-21","MANKIND","2026-09-21 05:26:13.001318+00","LONG",2425.5,2392.93),
    ("2026-09-21","PATANJALI","2026-09-21 04:41:11.057909+00","LONG",386.35,382.54),
    ("2026-09-21","PCBL","2026-09-21 04:30:08.958204+00","LONG",351.9,345.78),
    ("2026-09-21","TEGA","2026-09-21 04:38:02.738267+00","LONG",2084.0,2052.53),
    ("2026-09-21","WOCKPHARMA","2026-09-21 04:47:54.680805+00","LONG",2192.0,2162.4),
    ("2026-09-22","GABRIEL","2026-09-22 08:57:02.959734+00","LONG",1451.3,1435.28),
    ("2026-09-22","JSWINFRA","2026-09-22 04:34:03.332877+00","LONG",365.85,360.17),
    ("2026-09-22","MEESHO","2026-09-22 04:37:20.052989+00","LONG",228.88,225.83),
]


def _walk(pos: dict, bars: list, policy: dict) -> tuple[str, float]:
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
        d = evaluate_intraday_exit(pos, float(bar.close), policy, now=now,
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


def _gross_r(entry, exit_price, stop, d):
    risk = D.risk_per_share(entry, stop, d) or entry * 0.005
    return D.gain_r(entry, exit_price, risk, d)


VARIANTS = [
    ("1.5R / gb=30% (real)", 1.5, 30.0),
    ("2.0R / gb=30%",         2.0, 30.0),
    ("2.5R / gb=30%",         2.5, 30.0),
    ("1.5R / gb=50%",         1.5, 50.0),
    ("2.5R / gb=50%",         2.5, 50.0),
    ("2.5R / gb=off",         2.5, 0.0),
    ("3.0R / gb=off",         3.0, 0.0),
]


def run():
    sb = get_supabase()
    src = BarSource(kite=get_kite())
    base_policy = load_intraday_policy(engine="IGN")  # real live IGN policy today

    rows = []
    for day, sym, ts_str, direction, entry, stop in IGN_DETECTIONS:
        bars = src.get(sym, day)
        if not bars:
            print(f"  {sym} {day}: no bars, skipped")
            continue
        det_ts = datetime.fromisoformat(ts_str)
        walk_bars = [b for b in bars if b.ts >= det_ts]
        if len(walk_bars) < 2:
            print(f"  {sym} {day}: <2 bars after detection, skipped")
            continue
        risk = D.risk_per_share(entry, stop, direction) or entry * 0.005
        results = {}
        for label, r_mult, gb_pct in VARIANTS:
            target = entry + D.sign(direction) * risk * r_mult
            pos = {"entry_price": entry, "planned_stop": stop, "planned_target": target,
                   "direction": direction, "active_sl": stop, "high_water_mark": entry}
            policy = dict(base_policy)
            policy["giveback_pct"] = gb_pct
            action, exit_price = _walk(pos, walk_bars, policy)
            results[label] = (action, _gross_r(entry, exit_price, stop, direction))
        rows.append((sym, day, direction, results))

    labels = [v[0] for v in VARIANTS]
    print(f"\n{'symbol':<12}{'date':<12}{'dir':<6}" + "".join(f"{l:<12}" for l in labels))
    for sym, day, direction, results in rows:
        line = f"{sym:<12}{day:<12}{direction:<6}"
        for label in labels:
            _, r = results[label]
            line += f"{r:+.2f}R     "
        print(line)

    print(f"\n{'='*74}\nSUMMARY -- n={len(rows)} real IGN detections, real bars, real ladder\n{'='*74}")
    for label in labels:
        rs = [results[label][1] for _, _, _, results in rows]
        mean = statistics.mean(rs)
        med = statistics.median(rs)
        wins = sum(1 for r in rs if r > 0)
        print(f"  {label:<24} mean={mean:+.4f}R  median={med:+.4f}R  win={wins}/{len(rs)}={wins/len(rs):.0%}")


if __name__ == "__main__":
    run()
