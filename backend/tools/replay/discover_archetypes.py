"""
Discovery screen: instead of retuning IGN's one hypothesis (ignition momentum), scan the full local archive for
whether OTHER established intraday concepts show a real edge. Six families, long and short where the concept
is naturally two-sided, 14 trigger cells total:

    ORB5 / ORB15    opening-range breakout (Crabel/Connors) — break above/below the first 5 or 15 minutes' range
    VWAP_REV        mean reversion off a stretch from the running VWAP (institutional desk playbook)
    GAP_*_CONTINUE  a significant overnight gap extends through the morning
    GAP_*_FADE      a significant overnight gap reverses
    PDHL_BREAK      the prior session's high/low is broken with a same-day close beyond it
    RSI2            Connors' 2-period RSI extreme (adapted to an intraday entry, not swing)

Detection is vectorised across a symbol's whole history at once, the same approach as ign_archive_events.py, and
reuses its liquidity floor, gap/session filters and per-event feature builder (row_features) so a discovered
event carries the identical feature set an IGN event does — the day-clustered search and walk-forward in
ign_event_study.py need no changes to run on this table; only the trigger definitions here are new.

    python -m tools.replay.discover_archetypes build             # writes the train/holdout tables
    python -m tools.replay.discover_archetypes build --limit 50  # dry run
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

from loguru import logger as log

from tools.kite_history import store as KH
from tools.replay import ign_archive_events as A
from tools.replay import ign_event_features as F
from tools.replay import ign_event_sim as SIM
from tools.replay.ign_event_table import MAX_GAP_PCT, MIN_IDX, MIN_SESSION_BARS, REASON_CODE, core_policies
from tools.replay.ign_feature_stats import MIN_PRICE, MIN_TURNOVER_CR

ROOT = KH.DEFAULT_ROOT
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
TRAIN_TABLE = CACHE / "discover_table_train.jsonl"
HOLDOUT_TABLE = CACHE / "discover_table_holdout.jsonl"

STUDY_START = "2016-01-01"
TRAIN_END = "2025-09-30"            # a FRESH holdout: 2025-10-01..now was never opened for the IGN study
GAP_MIN_PCT = 2.0
BREAK_BUFFER = 0.0015               # 0.15%: a break must clear the level, not tie it
PDHL_BUFFER = 0.002                 # 0.2%: the prior-day level, a further break, needs a clearer margin
VWAP_STRETCH_PCT = 1.5              # tighter than the ~1.0% touched routinely, closer to a real dislocation
ORB_MIN_VR = 1.0                    # IGN's own volume ratio (already computed by _minute_frame): a breakout on
                                     # dead volume is not the same event as one with real participation
RSI2_LOW, RSI2_HIGH = 10.0, 90.0    # Connors' own "under 5-10 / over 90-95" extreme, not merely oversold
CTRL_PER_SYMBOL_PER_YEAR = 2
CTRL_IDX_RANGE = (15, 330)
CTRL_SEED = 20261229


def _rsi2_prior(daily: list, n: int = 2) -> np.ndarray:
    """Wilder RSI(n) on daily closes, lagged one session (index p holds the RSI known at the close of
    daily[p-1]) — the same recursive formula as intraday.trend_indicators.rsi, computed in one pass instead
    of once per candidate day. NaN until n+1 prior closes exist."""
    closes = np.array([b.close for b in daily], dtype=float)
    out = np.full(len(closes), np.nan)
    if len(closes) < n + 2:
        return out
    diffs = np.diff(closes)
    gains, losses = np.clip(diffs, 0, None), np.clip(-diffs, 0, None)
    ag, al = gains[:n].mean(), losses[:n].mean()
    out[n] = 100.0 if al == 0 and ag > 0 else (50.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    for i in range(n + 1, len(closes)):
        g, l = gains[i - 1], losses[i - 1]
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
        out[i] = 100.0 if al == 0 and ag > 0 else (50.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return np.concatenate([[np.nan], out[:-1]])


def _first_per_day(df: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    return df[mask].groupby("date", sort=False, as_index=False).head(1)


def enrich_frame(mdf: pd.DataFrame, daily: list) -> pd.DataFrame:
    """ign_archive_events' base minute frame (idx, cumvol, chg_pct, vr, prev_close/prev_vol) plus the columns
    the archetypes below need: running VWAP deviation, the 5- and 15-minute opening range, the day's own gap,
    and the prior session's high/low. Every added column is causal — built only from bars up to and including
    the row it sits on, or from the PRIOR day's daily bar."""
    pos = {b.date: p for p, b in enumerate(daily)}
    prev_close = {b.date: daily[p - 1].close for b in daily for p in (pos[b.date],) if p >= 1}
    prev_vol = {b.date: daily[p - 1].volume for b in daily for p in (pos[b.date],) if p >= 1}
    prior_high = {b.date: daily[p - 1].high for b in daily for p in (pos[b.date],) if p >= 1}
    prior_low = {b.date: daily[p - 1].low for b in daily for p in (pos[b.date],) if p >= 1}
    df = A._minute_frame(mdf, prev_close, prev_vol)
    if df.empty:
        return df
    g = df.groupby("date", sort=False)
    typ = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_tpv = (typ * df["volume"]).groupby(df["date"], sort=False).cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = cum_tpv / df["cumvol"]
    df["vwap_dev_pct"] = (df["close"] / vwap - 1.0) * 100.0
    for k in (5, 15):
        first_k = df[df["idx"] < k].groupby("date", sort=False).agg(**{f"or{k}_h": ("high", "max"), f"or{k}_l": ("low", "min")})
        df = df.merge(first_k, on="date", how="left")
    open0 = df[df["idx"] == 0].set_index("date")["open"]
    df["open0"] = df["date"].map(open0)
    df["gap_pct"] = (df["open0"] / df["prev_close"] - 1.0) * 100.0
    df["prior_high"] = df["date"].map(prior_high)
    df["prior_low"] = df["date"].map(prior_low)
    return df


def scan_archetypes(mdf: pd.DataFrame, daily: list, eligible: set, rsi2_prior: np.ndarray) -> dict:
    """{date: {idx: [cell names]}} for all 14 discovery cells across a symbol's whole history."""
    pos = {b.date: p for p, b in enumerate(daily)}
    df = enrich_frame(mdf, daily)
    if df.empty:
        return {}
    df = df[df["date"].isin(eligible)]
    if df.empty:
        return {}
    day_bar_count = df.groupby("date")["idx"].transform("size")
    df = df[day_bar_count >= MIN_SESSION_BARS]
    gap0 = (df["idx"] == 0)
    bad_gap_days = set(df.loc[gap0 & (df["chg_pct"].abs() > MAX_GAP_PCT), "date"])
    if bad_gap_days:
        df = df[~df["date"].isin(bad_gap_days)]
    if df.empty:
        return {}

    triggers: dict = {}

    def record(day, idx, name):
        triggers.setdefault(day, {}).setdefault(int(idx), []).append(name)

    def emit(name, mask):
        for r in _first_per_day(df, mask).itertuples():
            record(r.date, r.idx, name)

    have_vol = df["vr"] >= ORB_MIN_VR
    for k in (5, 15):
        h, l = df[f"or{k}_h"], df[f"or{k}_l"]
        base = (df["idx"] >= k) & h.notna() & l.notna() & have_vol
        emit(f"ORB{k}_LONG", base & (df["close"] > h * (1 + BREAK_BUFFER)))
        emit(f"ORB{k}_SHORT", base & (df["close"] < l * (1 - BREAK_BUFFER)))

    vbase = df["idx"] >= MIN_IDX
    emit("VWAP_REV_LONG", vbase & (df["vwap_dev_pct"] <= -VWAP_STRETCH_PCT))
    emit("VWAP_REV_SHORT", vbase & (df["vwap_dev_pct"] >= VWAP_STRETCH_PCT))

    gbase = df["idx"] >= 3
    up, down = df["gap_pct"] >= GAP_MIN_PCT, df["gap_pct"] <= -GAP_MIN_PCT
    hold_up, hold_down = df["close"] >= df["open0"], df["close"] <= df["open0"]
    fade_up = df["close"] < df["open0"] * (1 - BREAK_BUFFER)
    fade_down = df["close"] > df["open0"] * (1 + BREAK_BUFFER)
    emit("GAP_UP_CONTINUE", gbase & up & hold_up)
    emit("GAP_DOWN_CONTINUE", gbase & down & hold_down)
    emit("GAP_UP_FADE", gbase & up & fade_up)
    emit("GAP_DOWN_FADE", gbase & down & fade_down)

    pbase = (df["idx"] >= MIN_IDX) & df["prior_high"].notna() & df["prior_low"].notna() & have_vol
    emit("PDHL_BREAK_LONG", pbase & (df["close"] > df["prior_high"] * (1 + PDHL_BUFFER)))
    emit("PDHL_BREAK_SHORT", pbase & (df["close"] < df["prior_low"] * (1 - PDHL_BUFFER)))

    rsi_long = {daily[p].date for p in range(len(daily)) if np.isfinite(rsi2_prior[p]) and rsi2_prior[p] <= RSI2_LOW}
    rsi_short = {daily[p].date for p in range(len(daily)) if np.isfinite(rsi2_prior[p]) and rsi2_prior[p] >= RSI2_HIGH}
    emit("RSI2_LONG", (df["idx"] == 0) & df["date"].isin(rsi_long))
    emit("RSI2_SHORT", (df["idx"] == 0) & df["date"].isin(rsi_short))
    return triggers


def rank_by_recent_turnover(root, symbols: list[str], lookback_days: int = 60) -> list[str]:
    """Symbols ordered by their own most recent `lookback_days` average daily turnover (price x volume),
    richest first. A quick daily-only pass (small files) so `--top` can bound the minute-level scan to the
    names that actually matter, instead of spending most of the run's time on the long illiquid tail."""
    scored = []
    for sym in symbols:
        d = KH.read(root, "day", "NSE", sym, start="2020-01-01")
        if d.empty:
            continue
        tail = d.tail(lookback_days)
        turnover_cr = float((tail["close"] * tail["volume"]).mean()) / 1e7
        if turnover_cr > 0:
            scored.append((sym, turnover_cr))
    scored.sort(key=lambda t: -t[1])
    return [s for s, _ in scored]


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


def _checkpoint(rows: dict, pol: dict, names: list, limit: int | None) -> None:
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


def build(limit: int | None = None, top: int | None = None) -> None:
    root = ROOT
    symbols = KH.list_symbols(root, "minute", "NSE")
    if top:
        t0r = time.time()
        symbols = rank_by_recent_turnover(root, symbols)[:top]
        print(f"ranked by recent turnover, top {top} kept ({(time.time() - t0r) / 60:.1f} min)", flush=True)
    if limit:
        symbols = symbols[:limit]
    print(f"{len(symbols)} NSE symbols, study window {STUDY_START}..latest, train_end {TRAIN_END} (fresh holdout)", flush=True)
    rng = np.random.default_rng(CTRL_SEED)
    t0 = time.time()
    profile = A.build_profile(root, symbols, rng)
    print(f"  profile built ({(time.time() - t0) / 60:.1f} min)", flush=True)
    idx_days, idx_daily = A.load_indices(root)
    # a coarse screen, not the deep-dive: plain next-open entries only, no pullback/time-stop/breakeven
    # variants — about a third of the full 92-policy grid. If an archetype survives this, ITS rows alone
    # (a small subset) get the full grid re-run for the actual filter search; scanning all 2,673 symbols'
    # whole history with all 92 policies on every one of ~2.6M candidate events does not fit a session.
    full_pols = core_policies()
    pols = {n: p for n, p in full_pols.items() if "|nx|" in n and n.endswith("|m-|b-")}
    names = list(pols)
    print(f"  coarse policy grid: {len(names)} of {len(full_pols)}", flush=True)
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
            daily = A._daily_bars(ddf)
            if len(daily) < 30:
                skipped["short_history"] = skipped.get("short_history", 0) + 1
                continue
            pos = {b.date: p for p, b in enumerate(daily)}
            rsi2_prior = _rsi2_prior(daily)
            eligible = {daily[p].date for p in range(1, len(daily))
                        if daily[p - 1].close >= MIN_PRICE and daily[p - 1].close * daily[p - 1].volume / 1e7 >= MIN_TURNOVER_CR}
            triggers = scan_archetypes(mdf, daily, eligible, rsi2_prior)
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
            dfeat_cache: dict = {}          # daily_features(pos) is identical for every idx that fires on the same day
            for day, by_idx in triggers.items():
                p = pos.get(day)
                if p is None or p < 25:
                    continue
                g = mdf_by_date.get(day)
                if g is None or len(g) < MIN_SESSION_BARS:
                    continue
                d = F.DayBars(g["open"].to_numpy(float), g["high"].to_numpy(float), g["low"].to_numpy(float),
                             g["close"].to_numpy(float), g["volume"].to_numpy(float))
                if len(d.c) <= max(by_idx):
                    skipped["bar_count_mismatch"] = skipped.get("bar_count_mismatch", 0) + 1
                    continue
                for i, cell_names in by_idx.items():
                    try:
                        got = A.row_features(sym, day, d, i, daily, p, idx_days, idx_daily, profile, dfeat_cache)
                    except Exception as e:
                        skipped["feature_error"] = skipped.get("feature_error", 0) + 1
                        log.warning(f"  {sym} {day} idx={i}: {type(e).__name__}: {e}")
                        continue
                    if got is None:
                        continue
                    base_row, _ = got
                    split = "train" if day.isoformat() <= TRAIN_END else "holdout"
                    # simulate() depends only on (d, i, policy), never on which archetype labelled this bar —
                    # compute the 92-policy grid ONCE per bar and reuse it for every cell sharing that bar
                    # (about 1 in 4 bars here has 2+ labels: two ORB windows, or a gap that is also a VWAP stretch)
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
                    for name in cell_names:
                        r = dict(base_row, cell=name, kind=("control" if name == "CTRL" else "event"))
                        rows[split].append(r)
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
    b.add_argument("--top", type=int, default=None, help="keep only the N symbols with the highest recent turnover")
    args = p.parse_args()
    if args.cmd == "build":
        build(args.limit, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
