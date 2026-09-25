"""
The IGN event table — one row per (symbol-day, trigger cell), features as of the trigger bar and
outcomes from the next bar, built from the cached Kite minute and daily bars.

    python -m tools.replay.ign_event_table build                 # writes train + holdout tables
    python -m tools.replay.ign_event_table build --limit 200     # dry run on the first 200 symbol-days

TRIGGER CELLS. A cell is (move >= T% over the prior close, IGN volume ratio >= V), first bar that
satisfies both with at least 8 bars seen. The cells span the live rule and its neighbours so a
threshold can be STUDIED rather than assumed; `LIVE` is the live rule itself (move >= max(3.5%,
1.2 x daily ATR%), ratio >= 2.0, structural stop within 1.75%). `CTRL` rows are random bars on
random symbol-days: the benchmark for "enter any liquid stock at any time".

TWO FILES, ON PURPOSE. Rows from the sealed holdout window go to a separate file that exploration
code never opens; `load_train` cannot return them and asserts that no holdout day is in what it read.

No decision input comes from anywhere but Kite: every price and volume is a Kite minute or daily
bar. (`delivery_pct`, the one permitted exception, is not used here.)
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import date as _date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.replay import bars as _bars
from tools.replay import ign_event_features as F
from tools.replay import ign_event_sim as SIM
from tools.replay.ign_event_data import INDICES, LISTS
from tools.replay.ign_feature_study import DAILY_DIR

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
TRAIN_TABLE = CACHE / "ign_event_table_train.jsonl"
HOLDOUT_TABLE = CACHE / "ign_event_table_holdout.jsonl"

TRAIN_END = "2026-07-17"           # holdout = every session after this
CELL_T = (3.0, 3.5, 4.0, 5.0)
CELL_V = (0.0, 1.0, 2.0, 3.0)
MIN_IDX = F.MIN_BARS - 1
CTRL_PER_DAY = 3
CTRL_IDX_RANGE = (15, 330)
CTRL_SEED = 20260926
MIN_SESSION_BARS = 300             # a shorter day is a data gap or a special session
MAX_GAP_PCT = 25.0                 # a bigger open gap is a corporate action or a listing, not a signal


def split_of(day: str) -> str:
    return "train" if day <= TRAIN_END else "holdout"


def cell_name(t: float, v: float) -> str:
    return f"T{t:g}_V{v:g}"


def cells() -> list[tuple[str, float, float]]:
    return [(cell_name(t, v), t, v) for t in CELL_T for v in CELL_V]


# ── the policy set the table carries results for ────────────────────────────

def _pname(p: SIM.Policy) -> str:
    e = "nx" if p.entry == "next_open" else f"pb{p.pb_pct:g}w{p.wait}"
    s = f"s{int(p.stop_arg)}" if p.stop == "struct" else f"p{p.stop_arg:g}"
    return "|".join([("L" if p.side == 1 else "S"), e, s,
                     f"t{p.target_r:g}" if p.target_r else "t-",
                     f"m{p.max_bars}" if p.max_bars else "m-",
                     f"b{p.be_after_r:g}" if p.be_after_r else "b-"])


def core_policies() -> dict[str, SIM.Policy]:
    out: dict[str, SIM.Policy] = {}

    def add(p):
        out[_pname(p)] = p

    for stop, arg in (("struct", 20), ("pct", 1.0), ("pct", 1.5)):
        for tgt in (None, 0.5, 1.0, 1.5, 2.0, 3.0):
            for mb in (None, 60):
                for be in (None, 1.0):
                    if be and tgt and tgt <= be:
                        continue
                    add(SIM.Policy(side=1, stop=stop, stop_arg=arg, target_r=tgt, max_bars=mb, be_after_r=be))
    for pb, w in ((0.3, 10), (0.6, 20)):
        for stop, arg in (("struct", 20), ("pct", 1.0)):
            for tgt in (1.0, 2.0):
                add(SIM.Policy(side=1, entry="pullback", pb_pct=pb, wait=w, stop=stop, stop_arg=arg, target_r=tgt))
    for stop, arg in (("struct", 20), ("pct", 1.0), ("pct", 1.5)):       # the FADE: short the spike
        for tgt in (0.5, 1.0, 1.5, 2.0):
            for mb in (None, 60):
                add(SIM.Policy(side=-1, stop=stop, stop_arg=arg, target_r=tgt, max_bars=mb))
    return out


REASON_CODE = {"STOP": 1, "TARGET": 2, "TIME": 3, "EOD": 4}


# ── inputs ──────────────────────────────────────────────────────────────────

def _load_daily(sym: str):
    p = DAILY_DIR / f"{sym.replace('/', '_')}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def _load_index_daily(name: str) -> list[dict]:
    p = DAILY_DIR / f"__{name.replace(' ', '_')}.json.gz"
    if not p.exists():
        return []
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def day_bars(sym: str, day: str) -> F.DayBars | None:
    b = _bars.load_cached(sym, day, "minute")
    if not b:
        return None
    return F.day_from_rows([{"open": x.open, "high": x.high, "low": x.low,
                             "close": x.close, "volume": x.volume} for x in b])


def _ts(idx: int) -> str:
    m = 9 * 60 + 16 + idx
    return f"{m // 60:02d}:{m % 60:02d}"


# ── one symbol-day ──────────────────────────────────────────────────────────

class Ctx:
    """Everything shared across symbol-days: index bars, index dailies, the volume profile."""

    def __init__(self, profile: np.ndarray):
        self.profile = profile
        self.idx_daily = {n: _load_index_daily(n) for n in INDICES}
        self._raw: dict[str, list | None] = {}
        self._idx_bars: dict[tuple, F.DayBars | None] = {}

    def raw(self, sym: str):
        if sym not in self._raw:
            self._raw[sym] = _load_daily(sym)
        return self._raw[sym]

    def index_bars(self, name: str, day: str):
        """An index's minute bars for one day, kept: every symbol-day on that day reads them."""
        key = (name, day)
        if key not in self._idx_bars:
            self._idx_bars[key] = day_bars(name, day)
        return self._idx_bars[key]


def row_features(ctx: Ctx, sym: str, day: str, d: F.DayBars, i: int, daily_bars: list,
                 dfeat: dict, idx_bars: dict, idx_prev: dict) -> dict:
    prev_close, prev_vol = dfeat["prev_close"], dfeat["prev_day_volume"]
    f = F.intraday_features(d, i, prev_close, prev_vol, dfeat["avg20_volume"], ctx.profile)
    f.update(F.market_features(idx_bars.get("NIFTY 50"), idx_bars.get("NIFTY 500"), idx_bars.get("INDIA VIX"),
                               i, idx_prev["NIFTY 50"].get("prev_close"), idx_prev["NIFTY 500"].get("prev_close"),
                               idx_prev["INDIA VIX"].get("prev_close"), f["move_pct"]))
    for name, key in (("NIFTY 50", "nifty"), ("NIFTY 500", "n500")):
        for k in ("dist_sma20", "ret_5d", "ret_20d"):
            f[f"{key}_{k}"] = idx_prev[name].get(k)
    f.update({k: v for k, v in dfeat.items() if k not in ("prev_close", "prev_day_volume")})
    f["prev_close"], f["prev_day_volume"] = prev_close, prev_vol
    f["hhmm"] = _ts(i)
    return f


def symbol_day_rows(ctx: Ctx, sym: str, day: str, kind: str, rng) -> tuple[list[dict], list[tuple[int, F.DayBars]]] | str:
    """Rows for one symbol-day, plus (row_index_in_list -> (signal idx, DayBars)) for the policy
    pass. Returns a short reason string when the day cannot be used."""
    d = day_bars(sym, day)
    if d is None:
        return "no_minute_bars"
    if d.n < MIN_SESSION_BARS:
        return "short_session"
    raw = ctx.raw(sym)
    if not raw:
        return "no_daily"
    from intraday.daily_history import to_daily_bars
    dbars = to_daily_bars([dict(r, date=_date.fromisoformat(r["date"]) if isinstance(r["date"], str) else r["date"])
                           for r in raw], _date.fromisoformat(day))
    if len(dbars) < 25:
        return "short_history"
    dfeat = F.daily_features(dbars)
    dfeat["prev_close"] = float(dbars[-1].close)
    prev_close, prev_vol = dfeat["prev_close"], dfeat["prev_day_volume"]
    if prev_close <= 0 or prev_vol <= 0:
        return "bad_prior_session"
    gap = (float(d.o[0]) / prev_close - 1.0) * 100.0
    if abs(gap) > MAX_GAP_PCT:
        return "corporate_action_gap"
    idx_bars = {n: ctx.index_bars(n, day) for n in INDICES}
    idx_prev = {n: F.index_daily_features(ctx.idx_daily[n], day) for n in INDICES}
    atr = dfeat.get("atr14_pct") or 2.0

    triggers: dict[int, list[str]] = {}
    if kind == "event":
        for name, t, v in cells():
            i = F.first_trigger(d, prev_close, prev_vol, t, v, min_idx=MIN_IDX)
            if i is not None:
                triggers.setdefault(i, []).append(name)
        live = F.first_trigger(d, prev_close, prev_vol, max(3.5, 1.2 * atr), 2.0, min_idx=MIN_IDX,
                               require_feasible=True)
        if live is not None:
            triggers.setdefault(live, []).append("LIVE")
    else:
        lo, hi = CTRL_IDX_RANGE
        for i in sorted(int(x) for x in rng.choice(np.arange(lo, min(hi, d.n - 2)), CTRL_PER_DAY, replace=False)):
            triggers.setdefault(i, []).append("CTRL")
    rows, jobs = [], []
    for i, names in sorted(triggers.items()):
        feats = row_features(ctx, sym, day, d, i, dbars, dfeat, idx_bars, idx_prev)
        fo = F.forward_outcomes(d, i) or {}
        for name in names:
            r = {"symbol": sym, "day": day, "cell": name, "kind": kind, "weekday": _date.fromisoformat(day).weekday()}
            r.update(feats)
            r.update({f"fo_{k}": v for k, v in fo.items()})
            r["fo_ok"] = bool(fo)
            r["atr_gate_pct"] = max(3.5, 1.2 * atr)
            rows.append(r)
            jobs.append((i, d))
    return rows, jobs


# ── the build ───────────────────────────────────────────────────────────────

def _control_profile(lists: dict) -> np.ndarray:
    days = []
    for sym, day in lists["controls"]:
        if day > TRAIN_END:
            continue
        d = day_bars(sym, day)
        if d is not None and d.n >= F.SESSION_MIN - 5:
            days.append(d)
        if len(days) >= 1500:
            break
    if len(days) < 200:
        raise SystemExit(f"only {len(days)} full control sessions cached — collection is not finished")
    return F.volume_profile(days)


def build(limit: int | None = None) -> None:
    lists = json.loads(LISTS.read_text(encoding="utf-8"))
    ctx = Ctx(_control_profile(lists))
    pols = core_policies()
    names = list(pols)
    rng = np.random.default_rng(CTRL_SEED)
    ev = [("event", s, dd) for s, dd in lists["events"]]
    ct = [("control", s, dd) for s, dd in lists["controls"]]
    work = (ev[:limit] + ct[:limit]) if limit else ev + ct
    print(f"{len(work)} symbol-days, {len(names)} policies, profile from controls (train only)", flush=True)
    outs = {"train": [], "holdout": []}
    pol_res = {"train": [], "holdout": []}
    skipped: dict[str, int] = {}
    t0 = time.time()
    for n, (kind, sym, day) in enumerate(work, 1):
        got = symbol_day_rows(ctx, sym, day, kind, rng)
        if isinstance(got, str):
            skipped[got] = skipped.get(got, 0) + 1
            continue
        rows, jobs = got
        split = split_of(day)
        for r, (i, d) in zip(rows, jobs):
            g = np.full(len(names), np.nan, dtype=np.float32)
            k = np.full(len(names), np.nan, dtype=np.float32)
            b = np.full(len(names), -1, dtype=np.int16)
            c = np.zeros(len(names), dtype=np.int8)
            for j, nm in enumerate(names):
                res = SIM.simulate(d, i, pols[nm])
                if res is not None:
                    g[j], k[j], b[j], c[j] = res["gross_pct"], res["risk_pct"], res["bars"], REASON_CODE[res["reason"]]
            outs[split].append(r)
            pol_res[split].append((g, k, b, c))
        if n % 2000 == 0:
            print(f"  {n}/{len(work)}  {time.time() - t0:.0f}s  rows {len(outs['train'])}+{len(outs['holdout'])}  "
                  f"skipped {skipped}", flush=True)
    for split, path in (("train", TRAIN_TABLE), ("holdout", HOLDOUT_TABLE)):
        if limit:
            path = path.with_name(path.stem + f"_limit{limit}" + path.suffix)
        with path.open("w", encoding="utf-8") as fh:
            for r in outs[split]:
                fh.write(json.dumps(r, default=float) + "\n")
        if pol_res[split]:
            g, k, b, c = (np.stack(x) for x in zip(*pol_res[split]))
            np.savez_compressed(path.with_suffix(".policies.npz"), gross=g, risk=k, bars=b, reason=c,
                                names=np.array(names))
        print(f"{split}: {len(outs[split])} rows -> {path.name}", flush=True)
    print("skipped:", skipped, flush=True)


def load_train():
    """Train rows and their policy matrix. The holdout file is never opened here."""
    import pandas as pd
    df = pd.read_json(TRAIN_TABLE, lines=True)
    z = np.load(TRAIN_TABLE.with_suffix(".policies.npz"))
    assert len(df) == len(z["gross"]), "table and policy matrix are out of step"
    assert df["day"].max() <= TRAIN_END, "a holdout day leaked into the train table"
    return df, {k: z[k] for k in ("gross", "risk", "bars", "reason")}, [str(x) for x in z["names"]]


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
