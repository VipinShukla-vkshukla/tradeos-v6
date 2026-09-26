"""
Integrity checks on what is stored: does each symbol's history look like a real, ordered, gap-explained
series? Nothing is repaired here; this only reports.

HARD errors (exit code 1 from the CLI): duplicate or out-of-order timestamps, high < low, open or close outside
[low, high], non-positive price, negative volume. Something is wrong with the file or with how it was built.

INFORMATION (not errors): candles outside 09:15-15:29 (special sessions such as the Diwali muhurat evening),
weekdays with no candles inside the symbol's own span (an illiquid stock that did not trade, or a holiday the
reference calendar lacks), and day-to-day close jumps over 25% (a corporate action Kite did not adjust for, or a
real limit move). Kite back-adjusts splits and bonuses itself (RELIANCE's 2015 prices are on today's scale) but not
everything, and a few bars are bad, so the jumps that remain are the ones to adjust or exclude before training on a
series.

`bars_mode` / `days_off_mode`: the usual number of candles per day and how many days differ from it. The usual number
is not constant across history (NSE equities went from 375 candles a day, 09:15-15:29, to 360, ending 15:14, on
2026-08-03, while indices still run to 15:29), so anything keyed to minute-of-day needs to know which regime it is in.
"""

from __future__ import annotations

from datetime import time as dtime
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from tools.kite_history import store

HARD = ("duplicate_ts", "unsorted", "high_lt_low", "ohlc_outside_range", "nonpositive_price", "negative_volume")
JUMP = 0.25


def verify_frame(df: pd.DataFrame, interval: str = "minute", calendar: set | None = None) -> dict:
    """Counts of each problem in one symbol's history. `calendar` = the set of dates the exchange traded."""
    out = {k: 0 for k in HARD}
    out.update({"rows": len(df), "days": 0, "outside_session": 0, "missing_days": 0, "big_day_jumps": [],
                "first": None, "last": None, "bars_mode": 0, "days_off_mode": 0})
    if df.empty:
        return out
    ts = df["ts"]
    out["first"], out["last"] = str(ts.iloc[0]), str(ts.iloc[-1])
    out["duplicate_ts"] = int(ts.duplicated().sum())
    out["unsorted"] = int((ts.diff().dropna() < pd.Timedelta(0)).sum())
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    out["high_lt_low"] = int((h < l - 1e-9).sum())
    out["ohlc_outside_range"] = int(((o > h + 1e-9) | (o < l - 1e-9) | (c > h + 1e-9) | (c < l - 1e-9)).sum())
    out["nonpositive_price"] = int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum())
    out["negative_volume"] = int((df["volume"].to_numpy() < 0).sum())
    days = ts.dt.date
    out["days"] = int(days.nunique())
    per_day = df.groupby(days).size()
    out["bars_mode"] = int(per_day.mode().iloc[0])
    out["days_off_mode"] = int((per_day != out["bars_mode"]).sum())
    if interval != "day":
        tod = ts.dt.time
        out["outside_session"] = int(((tod < dtime(9, 15)) | (tod > dtime(15, 29))).sum())
    daily_close = df.groupby(days)["close"].last()
    if len(daily_close) > 1:
        r = daily_close.pct_change().abs()
        out["big_day_jumps"] = [str(d) for d in r[r > JUMP].index]
    if calendar:
        lo, hi = days.min(), days.max()
        have = set(days)
        out["missing_days"] = int(sum(1 for d in calendar if lo <= d <= hi and d not in have))
    return out


def calendar_from(root: Path, interval: str = "minute", segment: str = "INDICES", symbol: str = "NIFTY 50") -> set:
    """The days the exchange traded, taken from the Nifty 50's own candles."""
    df = store.read_raw(root, interval, segment, symbol)
    return set(df["ts"].dt.date) if not df.empty else set()


def verify_symbol(root: Path, interval: str, segment: str, symbol: str, calendar: set | None = None) -> dict:
    return verify_frame(store.read_raw(root, interval, segment, symbol), interval, calendar)


def has_hard_errors(report: dict) -> bool:
    return any(report[k] for k in HARD)


def verify_all(root: Path, interval: str, segments: Sequence[str], sample: int | None = None,
               seed: int = 0) -> dict[tuple[str, str], dict]:
    cal = calendar_from(root, interval) if interval != "day" else calendar_from(root, "day")
    out = {}
    for seg in segments:
        syms = store.list_symbols(root, interval, seg)
        if sample and len(syms) > sample:
            rng = np.random.default_rng(seed)
            syms = sorted(rng.choice(syms, sample, replace=False).tolist())
        for s in syms:
            out[(seg, s)] = verify_symbol(root, interval, seg, s, cal)
    return out
