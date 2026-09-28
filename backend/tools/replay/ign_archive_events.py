"""
IGN redesign, from scratch, on the full local Kite archive (D:\\kite_history) — 2016-2026, every NSE stock,
every trading day. Supersedes the earlier study's 20-month, pre-selected-day-list table with a continuous
scan of the whole archive: ~11x the calendar span, every market regime since 2016 (2018 NBFC crisis, 2020
COVID crash and recovery, 2021 retail boom, 2022 correction), and no dependence on Kite's live rate limit —
everything is already on disk.

Same trigger/feature/outcome/policy DEFINITIONS as the prior study (tools.replay.ign_event_features,
ign_event_sim, ign_event_table.core_policies) — that code is already tested against the live engine — but a
different, vectorised DETECTION path: this scans every day of a symbol's full history at once with numpy/
pandas instead of re-fetching a pre-built candidate list, because scanning is now the cheap operation (the
archive is local) where fetching used to be the expensive one (the API was).

    python -m tools.replay.ign_archive_events build             # writes the train/holdout tables
    python -m tools.replay.ign_archive_events build --limit 50  # dry run on the first 50 symbols

TRAIN_END = 2024-12-31. Holdout = 2025-01-01..latest (~21 months), sealed the same way as before: a separate
file, never opened by selection code.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from loguru import logger as log
from datetime import date as _date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from intraday.trend_indicators import DailyBar, wilder_atr
from tools.kite_history import store as KH
from tools.replay import ign_event_features as F
from tools.replay import ign_event_sim as SIM
from tools.replay.ign_event_table import (CELL_T, CELL_V, MAX_GAP_PCT, MIN_IDX, MIN_SESSION_BARS,
                                          REASON_CODE, cell_name, cells, core_policies)
from tools.replay.ign_feature_stats import MIN_PRICE, MIN_TURNOVER_CR

ROOT = KH.DEFAULT_ROOT
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
TRAIN_TABLE = CACHE / "ign_event_table_train.jsonl"
HOLDOUT_TABLE = CACHE / "ign_event_table_holdout.jsonl"

TRAIN_END = "2024-12-31"
STUDY_START = "2016-01-01"          # 2015 excluded: dirtiest year in the integrity pass (zero-price bars, listings)
LOOSE_T, LOOSE_V = 3.5, 2.0         # cheap superset filter for LIVE; the tested F.first_trigger settles it exactly
CTRL_PER_SYMBOL_PER_YEAR = 2
CTRL_IDX_RANGE = (15, 330)
CTRL_SEED = 20261227


def _daily_bars(df: pd.DataFrame) -> list[DailyBar]:
    """Kite daily rows -> DailyBar, bad (non-positive-price) sessions dropped, exactly as to_daily_bars does."""
    out = []
    for r in df.itertuples():
        o, h, l, c, v = r.open, r.high, r.low, r.close, r.volume
        if min(o, h, l, c) <= 0:
            continue
        out.append(DailyBar(r.ts.date(), float(o), float(h), float(l), float(c), float(v)))
    return out


def _atr_pct_prior(bars: list[DailyBar]) -> np.ndarray:
    """ATR%, as of the PRIOR session, aligned to `bars` (index p holds the ATR% known at the close of bars[p-1])."""
    atr = wilder_atr(bars, 14)
    closes = np.array([b.close for b in bars], dtype=float)
    pct = np.array([(a / c * 100.0) if a else np.nan for a, c in zip(atr, closes)])
    return np.concatenate([[np.nan], pct[:-1]])


def _minute_frame(mdf: pd.DataFrame, prev_close: dict, prev_vol: dict) -> pd.DataFrame:
    """One row per minute bar, with same-day cumulative volume/index and the prior session's close/volume
    mapped in, ready for a fully vectorised trigger scan across the symbol's whole history at once."""
    df = mdf.copy()
    df["date"] = df["ts"].dt.date
    df["prev_close"] = df["date"].map(prev_close)
    df["prev_vol"] = df["date"].map(prev_vol)
    df = df[df["prev_close"].notna() & (df["prev_close"] > 0)].reset_index(drop=True)
    g = df.groupby("date", sort=False)
    df["idx"] = g.cumcount()
    df["cumvol"] = g["volume"].cumsum()
    df["chg_pct"] = (df["close"] / df["prev_close"] - 1.0) * 100.0
    with np.errstate(divide="ignore", invalid="ignore"):
        df["vr"] = df["cumvol"] / (df["prev_vol"] * np.maximum(1, df["idx"]) / F.SESSION_MIN)
    return df


def _first_per_day(df: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    return df[mask].groupby("date", sort=False, as_index=False).head(1)


def scan_triggers(mdf: pd.DataFrame, daily: list[DailyBar], atr_prior: np.ndarray, eligible: set) -> dict:
    """{date: {idx: [cell names]}} for every cell across a symbol's ENTIRE history, vectorised. `daily[p]`'s
    prior-session ATR is `atr_prior[p]`; `pos` below maps each trading date to its position in `daily`."""
    pos = {b.date: p for p, b in enumerate(daily)}
    prev_close = {b.date: daily[p - 1].close for b in daily for p in (pos[b.date],) if p >= 1}
    prev_vol = {b.date: daily[p - 1].volume for b in daily for p in (pos[b.date],) if p >= 1}
    df = _minute_frame(mdf, prev_close, prev_vol)
    if df.empty:
        return {}
    df = df[df["date"].isin(eligible)]           # the same >=Rs50 price / >=Rs25 Cr prior-day-turnover floor
    if df.empty:                                  # as the earlier study: a stock too illiquid to fill is not a signal
        return {}
    day_bar_count = df.groupby("date")["idx"].transform("size")
    df = df[day_bar_count >= MIN_SESSION_BARS]
    gap = (df["idx"] == 0)                                         # the day's own open-vs-prior-close gap
    bad_gap_days = set(df.loc[gap & (df["chg_pct"].abs() > MAX_GAP_PCT), "date"])
    if bad_gap_days:
        df = df[~df["date"].isin(bad_gap_days)]
    if df.empty:
        return {}

    triggers: dict = {}

    def record(day, idx, name):
        triggers.setdefault(day, {}).setdefault(int(idx), []).append(name)

    for name, t, v in cells():
        mask = (df["chg_pct"] >= t) & (df["idx"] >= MIN_IDX) & ((v <= 0) | (df["vr"] >= v))
        for r in _first_per_day(df, mask).itertuples():
            record(r.date, r.idx, name)

    loose = (df["chg_pct"] >= LOOSE_T) & (df["idx"] >= MIN_IDX) & (df["vr"] >= LOOSE_V)
    loose_days = sorted(set(df.loc[loose, "date"]))
    for day in loose_days:
        p = pos.get(day)
        if p is None or p < 1 or not np.isfinite(atr_prior[p]):
            continue
        day_rows = df[df["date"] == day].sort_values("idx")
        d = F.DayBars(day_rows["open"].to_numpy(float), day_rows["high"].to_numpy(float),
                     day_rows["low"].to_numpy(float), day_rows["close"].to_numpy(float),
                     day_rows["volume"].to_numpy(float))
        floor = max(3.5, 1.2 * float(atr_prior[p]))
        i = F.first_trigger(d, prev_close[day], prev_vol[day], floor, 2.0, min_idx=MIN_IDX, require_feasible=True)
        if i is not None:
            record(day, i, "LIVE")
    return triggers


def sample_controls(mdf: pd.DataFrame, eligible: set, rng, n: int) -> dict:
    df = mdf.copy()
    df["date"] = df["ts"].dt.date
    counts = df.groupby("date").size()
    lo, hi = CTRL_IDX_RANGE
    ok_days = sorted(d for d in counts[counts >= MIN_SESSION_BARS].index if d in eligible)
    if not ok_days or n <= 0:
        return {}
    days = rng.choice(np.array(ok_days, dtype=object), size=min(n, len(ok_days)), replace=False)
    out: dict = {}
    for day in days:
        idx = int(rng.integers(lo, hi))
        out.setdefault(day, {}).setdefault(idx, []).append("CTRL")
    return out


def row_features(sym: str, day, d: F.DayBars, i: int, daily: list[DailyBar], pos: int,
                 idx_days: dict, idx_daily_rows: dict, profile: np.ndarray) -> tuple[dict, dict] | None:
    """One event's full feature row + its forward-outcome dict, or None if there isn't enough prior daily
    history yet. Mirrors ign_event_table.row_features/symbol_day_rows, sourced from the local archive."""
    bars = daily[:pos]
    if len(bars) < 25:
        return None
    dfeat = F.daily_features(bars)
    if not dfeat:
        return None
    prev_close, prev_vol = float(bars[-1].close), float(bars[-1].volume)
    feats = F.intraday_features(d, i, prev_close, prev_vol, dfeat.get("avg20_volume") or 0.0, profile)
    day_str = day.isoformat()
    idx_bars = {n: idx_days.get(n, {}).get(day) for n in ("NIFTY 50", "NIFTY 500", "INDIA VIX")}
    idx_prev = {n: idx_daily_rows.get(n, {}).get(day, {}) for n in ("NIFTY 50", "NIFTY 500", "INDIA VIX")}
    feats.update(F.market_features(idx_bars.get("NIFTY 50"), idx_bars.get("NIFTY 500"), idx_bars.get("INDIA VIX"), i,
                                   idx_prev["NIFTY 50"].get("prev_close"), idx_prev["NIFTY 500"].get("prev_close"),
                                   idx_prev["INDIA VIX"].get("prev_close"), feats["move_pct"]))
    for name, key in (("NIFTY 50", "nifty"), ("NIFTY 500", "n500")):
        for k in ("dist_sma20", "ret_5d", "ret_20d"):
            feats[f"{key}_{k}"] = idx_prev[name].get(k)
    feats.update({k: v for k, v in dfeat.items() if k not in ("prev_close", "prev_day_volume")})
    feats["prev_close"], feats["prev_day_volume"] = prev_close, prev_vol
    m = i + 1 + 9 * 60 + 15
    feats["hhmm"] = f"{m // 60:02d}:{m % 60:02d}"
    fo = F.forward_outcomes(d, i) or {}
    row = {"symbol": sym, "day": day_str, "weekday": day.weekday(), "idx": int(i)}
    row.update(feats)
    row.update({f"fo_{k}": v for k, v in fo.items()})
    row["fo_ok"] = bool(fo)
    row["atr_gate_pct"] = max(3.5, 1.2 * (dfeat.get("atr14_pct") or 2.0))
    return row, fo


# ── market context, precomputed once per index ──────────────────────────────

def precompute_index_daily(daily_df: pd.DataFrame) -> dict:
    """{date: {prev_close, dist_sma20, ret_5d, ret_20d}} for an index, vectorised, no look-ahead: every value
    at date D uses only sessions strictly before D. Matches ign_event_features.index_daily_features exactly,
    without re-scanning the whole series once per event."""
    df = daily_df.sort_values("ts").reset_index(drop=True)
    close = df["close"]
    prev_close = close.shift(1)
    sma20 = close.shift(1).rolling(20, min_periods=20).mean()
    out = pd.DataFrame({
        "date": df["ts"].dt.date,
        "prev_close": prev_close,
        "dist_sma20": (prev_close / sma20 - 1.0) * 100.0,
        "ret_5d": (prev_close / close.shift(6) - 1.0) * 100.0,
        "ret_20d": (prev_close / close.shift(21) - 1.0) * 100.0,
    })
    out = out[out["prev_close"].notna()]
    return {r.date: {"prev_close": r.prev_close, "dist_sma20": r.dist_sma20, "ret_5d": r.ret_5d, "ret_20d": r.ret_20d}
            for r in out.itertuples()}


def index_day_bars(minute_df: pd.DataFrame) -> dict:
    """{date: DayBars} for every session in an index's minute history."""
    out = {}
    df = minute_df.copy()
    df["date"] = df["ts"].dt.date
    for day, g in df.groupby("date", sort=False):
        out[day] = F.DayBars(g["open"].to_numpy(float), g["high"].to_numpy(float), g["low"].to_numpy(float),
                             g["close"].to_numpy(float), g["volume"].to_numpy(float))
    return out


def load_indices(root: Path) -> tuple[dict, dict]:
    """{name: {date: DayBars}}, {name: {date: daily-feature dict}} for the three market-context indices."""
    idx_days, idx_daily = {}, {}
    for name in ("NIFTY 50", "NIFTY 500", "INDIA VIX"):
        m = KH.read(root, "minute", "INDICES", name)
        d = KH.read(root, "day", "INDICES", name)
        idx_days[name] = index_day_bars(m) if not m.empty else {}
        idx_daily[name] = precompute_index_daily(d) if not d.empty else {}
    return idx_days, idx_daily


def build_profile(root: Path, symbols: list[str], rng, n: int = 400) -> np.ndarray:
    """A representative intraday volume profile from full (>=MIN_SESSION_BARS-bar) random sessions."""
    days_kept = []
    for sym in rng.choice(symbols, min(len(symbols), n * 3), replace=False):
        m = KH.read(root, "minute", "NSE", sym, start="2023-01-01")   # one recent year is enough, and far cheaper
        if m.empty:                                                   # than loading a symbol's whole 11-year file
            continue
        m = m.copy()
        m["date"] = m["ts"].dt.date
        counts = m.groupby("date").size()
        full_days = counts[counts >= F.SESSION_MIN - 5].index
        if len(full_days) == 0:
            continue
        day = rng.choice(full_days)
        g = m[m["date"] == day]
        days_kept.append(F.DayBars(g["open"].to_numpy(float), g["high"].to_numpy(float), g["low"].to_numpy(float),
                                   g["close"].to_numpy(float), g["volume"].to_numpy(float)))
        if len(days_kept) >= n:
            break
    return F.volume_profile(days_kept)


# ── the build ────────────────────────────────────────────────────────────────

def _checkpoint(rows: dict, pol: dict, names: list, limit: int | None) -> None:
    """Write what has been built so far. Cheap relative to a symbol's own scan cost, and means a crash loses
    at most one checkpoint interval instead of the whole run (as the first full run did, at the 48-minute mark)."""
    for split, path in (("train", TRAIN_TABLE), ("holdout", HOLDOUT_TABLE)):
        if limit:
            path = path.with_name(path.stem + f"_limit{limit}" + path.suffix)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for r in rows[split]:
                fh.write(json.dumps(r, default=float) + "\n")
        tmp.replace(path)
        if pol[split]:
            g, k, b, c = (np.stack(x) for x in zip(*pol[split]))
            tmp_npz = path.with_suffix(".policies.tmp.npz")
            np.savez_compressed(tmp_npz, gross=g, risk=k, bars=b, reason=c, names=np.array(names))
            tmp_npz.replace(path.with_suffix(".policies.npz"))


def build(limit: int | None = None) -> None:
    root = ROOT
    symbols = KH.list_symbols(root, "minute", "NSE")
    if limit:
        symbols = symbols[:limit]
    print(f"{len(symbols)} NSE symbols, study window {STUDY_START}..latest, train_end {TRAIN_END}", flush=True)
    rng = np.random.default_rng(CTRL_SEED)
    t0 = time.time()
    print("building the intraday volume profile...", flush=True)
    profile = build_profile(root, symbols, rng)
    print(f"  profile built ({(time.time() - t0) / 60:.1f} min)", flush=True)
    print("loading market context (Nifty 50 / Nifty 500 / India VIX)...", flush=True)
    idx_days, idx_daily = load_indices(root)
    pols = core_policies()
    names = list(pols)
    print(f"  setup done ({(time.time() - t0) / 60:.1f} min) — starting the per-symbol scan", flush=True)

    rows: dict[str, list] = {"train": [], "holdout": []}
    pol: dict[str, list] = {"train": [], "holdout": []}
    skipped: dict[str, int] = {}
    n_events = n_ctrl = 0
    for n, sym in enumerate(symbols, 1):
        try:
            mdf = KH.read(root, "minute", "NSE", sym, start=STUDY_START)
            ddf = KH.read(root, "day", "NSE", sym, start="2014-01-01")
            if mdf.empty or ddf.empty:
                skipped["no_data"] = skipped.get("no_data", 0) + 1
                continue
            daily = _daily_bars(ddf)
            if len(daily) < 30:
                skipped["short_history"] = skipped.get("short_history", 0) + 1
                continue
            pos = {b.date: p for p, b in enumerate(daily)}
            atr_prior = _atr_pct_prior(daily)
            eligible = {daily[p].date for p in range(1, len(daily))
                        if daily[p - 1].close >= MIN_PRICE
                        and daily[p - 1].close * daily[p - 1].volume / 1e7 >= MIN_TURNOVER_CR}
            triggers = scan_triggers(mdf, daily, atr_prior, eligible)
            span_years = max(1, (daily[-1].date - daily[0].date).days // 365)
            ctrl = sample_controls(mdf, eligible, rng, n=CTRL_PER_SYMBOL_PER_YEAR * span_years)
            for day, by_idx in ctrl.items():
                slot = triggers.setdefault(day, {})
                for i, nm in by_idx.items():
                    slot[i] = slot.get(i, []) + nm
            if not triggers:
                continue
            mdf2 = mdf.copy()
            mdf2["date"] = mdf2["ts"].dt.date
            mdf_by_date = {d: g for d, g in mdf2.groupby("date", sort=False)}
            for day, by_idx in triggers.items():
                p = pos.get(day)
                if p is None or p < 25:
                    continue
                g = mdf_by_date.get(day)
                if g is None or len(g) < MIN_SESSION_BARS:
                    continue
                d = F.DayBars(g["open"].to_numpy(float), g["high"].to_numpy(float), g["low"].to_numpy(float),
                             g["close"].to_numpy(float), g["volume"].to_numpy(float))
                if len(d.c) <= max(by_idx):                          # this day's bar count disagrees with the scan
                    skipped["bar_count_mismatch"] = skipped.get("bar_count_mismatch", 0) + 1
                    continue
                for i, cell_names in by_idx.items():
                    try:
                        got = row_features(sym, day, d, i, daily, p, idx_days, idx_daily, profile)
                    except Exception as e:
                        skipped["feature_error"] = skipped.get("feature_error", 0) + 1
                        log.warning(f"  {sym} {day} idx={i}: {type(e).__name__}: {e}")
                        continue
                    if got is None:
                        continue
                    base_row, _ = got
                    split = "train" if day.isoformat() <= TRAIN_END else "holdout"
                    for name in cell_names:
                        r = dict(base_row, cell=name, kind=("control" if name == "CTRL" else "event"))
                        rows[split].append(r)
                        g_arr = np.full(len(names), np.nan, dtype=np.float32)
                        k_arr = np.full(len(names), np.nan, dtype=np.float32)
                        b_arr = np.full(len(names), -1, dtype=np.int16)
                        c_arr = np.zeros(len(names), dtype=np.int8)
                        for j, nm in enumerate(names):
                            try:
                                res = SIM.simulate(d, i, pols[nm])
                            except Exception:
                                res = None
                            if res is not None:
                                g_arr[j], k_arr[j] = res["gross_pct"], res["risk_pct"]
                                b_arr[j], c_arr[j] = res["bars"], REASON_CODE[res["reason"]]
                        pol[split].append((g_arr, k_arr, b_arr, c_arr))
                        n_events += (name != "CTRL")
                        n_ctrl += (name == "CTRL")
            if n % 50 == 0:
                el = time.time() - t0
                print(f"  {n}/{len(symbols)} symbols  {el / 60:.1f} min  events {n_events}  ctrl {n_ctrl}  "
                      f"train rows {len(rows['train'])}  holdout rows {len(rows['holdout'])}  skipped {skipped}", flush=True)
            if n % 300 == 0:
                _checkpoint(rows, pol, names, limit)
        except Exception as e:
            skipped["symbol_error"] = skipped.get("symbol_error", 0) + 1
            log.warning(f"  {sym}: {type(e).__name__}: {e}")

    _checkpoint(rows, pol, names, limit)
    print(f"train: {len(rows['train'])} rows -> {TRAIN_TABLE.name}", flush=True)
    print(f"holdout: {len(rows['holdout'])} rows -> {HOLDOUT_TABLE.name}", flush=True)
    print(f"skipped: {skipped}  total events: {n_events}  total controls: {n_ctrl}  "
          f"({(time.time() - t0) / 60:.1f} min)", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    if args.cmd == "build":
        build(args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
