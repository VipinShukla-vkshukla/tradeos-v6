"""
IGN feature study — does any daily trend signal separate IGN's good entries from
its bad ones, measured on far more than 35 independent observations?

    python -m tools.replay.ign_feature_study collect --start 2026-03-02 --end 2026-09-23
    python -m tools.replay.ign_feature_study analyze                    # TRAIN only
    python -m tools.replay.ign_feature_study analyze --reveal-holdout   # one look

Pre-registration, decision rule and every statistic live in
tools/replay/ign_feature_stats.py. That file and this one must be COMMITTED and
unchanged before `--reveal-holdout` will run, and it will not run twice for the
same pair of file versions (same mechanism as tools/replay/holdout.py, applied to
this study's own code because its "parameters" are the hypotheses themselves).

WHAT IS REPLAYED
----------------
IGN's real rules (`IgnitionMomentum.evaluate`, unmodified live code) over real
Kite minute bars, through the existing harness (`replay_symbol_day`), for every
(symbol, day) that a LOOSE prefilter says could have fired. The exit ladder is
the live IGN policy. Gross R per first detection of a symbol-day.

  * `prev` (prior close/high/low, ATR%, prior-day volume) is built from Kite
    DAILY bars strictly before the day — the operator's Kite-only rule, and the
    only option: stock_data_daily keeps 8 sessions (migration 130).
  * The feature panel is `daily_history.build_panel` on those same bars, as-of
    the prior completed session. The same function the live daemon runs.
  * `price_history_yf` is used ONLY to decide which symbol-days are worth
    fetching minute bars for. It is dividend-adjusted, so it never supplies a
    decision input.
  * The universe is every liquid name that moved, not the live top-40/bench —
    a SUPERSET of what live could trade. `delivery_pct` cannot be replayed (no
    history) and is recorded prospectively by the live shadow instead.

Two populations per symbol-day, from ONE un-deduplicated replay pass:
  live     first LONG at/after 10:00 — what the armed open-hour gate lets through
  ungated  first LONG at any time    — used only to test the hour effect itself
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

import config as _cfgmod
from config import fetch_all, get_supabase
from intraday import direction as D
from intraday.daily_history import build_panel, fetch_daily, to_daily_bars
from intraday.exit_policy import load_intraday_policy
from kite.kite_client import get_kite
from tools.replay import bars as _bars_mod
from tools.replay import detect as _detect
from tools.replay import ign_feature_stats as S
from tools.replay.bars import BarSource
from tools.replay.ladder_variant_check import _gross_r, _qty_for, _walk

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
DATASET = CACHE / "ign_feature_dataset.jsonl"
DAILY_DIR = CACHE / "daily"
RESULTS = HERE / "results"
DAILY_LOOKBACK_DAYS = 430


@contextmanager
def _cfg_override(overrides: dict):
    """Merge onto the real live config; restore in finally (see
    ign_entry_exit_variant_check._cfg_override for why not tests.cfg_ctx)."""
    _cfgmod.cfg("_force_load", "")
    saved = dict(_cfgmod._sys_config or {})
    merged = dict(saved)
    merged.update({k: str(v) for k, v in overrides.items()})
    _cfgmod._sys_config = merged
    try:
        yield
    finally:
        _cfgmod._sys_config = saved


# ── daily data ──────────────────────────────────────────────────────────────

def load_price_history(sb, start: str, end: str) -> dict[str, list[dict]]:
    """price_history_yf rows grouped by symbol, date-ordered. Selection only."""
    lo = (_date.fromisoformat(start) - timedelta(days=45)).isoformat()
    rows = fetch_all(
        lambda: sb.table("price_history_yf").select("symbol,date,high,close,volume")
                  .gte("date", lo).lte("date", end),
        order_by="symbol,date")
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(r)
    return out


def _daily_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol.replace('/', '_')}.json.gz"


def daily_raw(kite, src: BarSource, symbol: str, need_start: _date, need_end: _date,
              spacing_s: float) -> list[dict]:
    """Kite daily candles covering [need_start, need_end], cached per symbol.
    Dates come back as `date` objects, the shape build_panel/to_daily_bars read."""
    path = _daily_path(symbol)
    if path.exists():
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                blob = json.load(fh)
            if (_date.fromisoformat(blob["start"]) <= need_start
                    and _date.fromisoformat(blob["end"]) >= need_end):
                return [dict(r, date=_date.fromisoformat(r["date"])) for r in blob["rows"]]
        except Exception as e:
            logger.warning(f"  daily cache unreadable for {symbol}: {e}")
    token = src.resolve_tokens([symbol]).get(symbol)
    if not token:
        return []
    raw = fetch_daily(kite, token, need_start, need_end, spacing_s=spacing_s)
    rows = []
    for r in raw:
        d = r["date"]
        d = d.date() if hasattr(d, "date") and callable(d.date) else d
        rows.append({"date": d, "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(r["close"]),
                     "volume": float(r.get("volume") or 0)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"start": need_start.isoformat(), "end": need_end.isoformat(),
                   "rows": [dict(r, date=r["date"].isoformat()) for r in rows]}, fh)
    return rows


# ── one symbol-day ──────────────────────────────────────────────────────────

def _walk_detection(det, day_bars, policy) -> tuple[float, str] | None:
    walk_bars = [b for b in day_bars if b.ts >= det.ts]
    if len(walk_bars) < 2:
        return None
    pos = {"entry_price": det.entry, "planned_stop": det.stop,
           "planned_target": det.target, "direction": det.direction,
           "active_sl": det.stop, "high_water_mark": det.entry,
           "current_qty": _qty_for(det.entry)}
    action, exit_price = _walk(pos, walk_bars, policy)
    return _gross_r(det.entry, exit_price, det.stop, det.direction), action


def process_symbol_day(sym: str, day: str, src: BarSource, raw: list[dict],
                       policy: dict) -> dict:
    """Never raises and never returns nothing: every symbol-day leaves a record
    with a status, so coverage is a number and not an impression."""
    rec: dict = {"symbol": sym, "day": day}
    day_bars = src.get(sym, day)
    if not day_bars:
        return dict(rec, status="no_bars")
    d = _date.fromisoformat(day)
    prev = S.prev_from_daily(to_daily_bars(raw, d), d)
    if prev is None:
        return dict(rec, status="no_prev")
    if not S.gap_ok(prev["close"], day_bars[0].open):
        return dict(rec, status="gap_guard",
                    gap_pct=round((day_bars[0].open / prev["close"] - 1) * 100, 2))
    with _cfg_override({"ign_exclude_open_hour_enabled": "false",
                        "ign_trend_gate_enabled": "false"}):
        dets = _detect.replay_symbol_day(sym, day, day_bars, prev=prev, dedup_pct=-1.0)
    dets = [x for x in dets if x.engine == "IGN"]
    rec["feats"] = build_panel(raw, d)
    rec["n_dets"] = len(dets)
    if not any(x.direction == "LONG" for x in dets):
        return dict(rec, status="no_long_detection")
    rec["status"] = "ok"
    for pop in ("live", "ungated"):
        det = S.first_detection(dets, pop)
        if det is None:
            continue
        walked = _walk_detection(det, day_bars, policy)
        if walked is None:
            continue
        r, action = walked
        rec[pop] = {"ts": det.ts.isoformat(), "entry": det.entry, "stop": det.stop,
                    "target": det.target, "r": round(r, 4), "action": action,
                    "hour": S.hour_bucket_of(det.ts),
                    "chg_pct": det.meta.get("chg_pct"),
                    "volume_ratio": det.meta.get("volume_ratio")}
    return rec


# ── collect ─────────────────────────────────────────────────────────────────

def _done_keys() -> set[tuple[str, str]]:
    if not DATASET.exists():
        return set()
    out = set()
    with DATASET.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                out.add((r["symbol"], r["day"]))
            except (ValueError, KeyError):
                continue
    return out


def collect(start: str, end: str, min_interval: float, limit: int | None,
            dry_run: bool) -> None:
    sb = get_supabase()
    hist = load_price_history(sb, start, end)
    cands = S.candidate_days(hist, start, end)
    if limit:
        cands = cands[:limit]
    done = _done_keys()
    todo = [c for c in cands if c not in done]
    symbols = sorted({s for s, _ in todo})
    print(f"candidates {len(cands)} symbol-days ({len(symbols)} symbols) in {start}..{end}; "
          f"already collected {len(cands) - len(todo)}; to fetch {len(todo)}")
    if dry_run:
        eta = len(todo) * min_interval / 60.0
        print(f"dry run — minute-bar fetch alone is ~{eta:.0f} min at {min_interval}s/call "
              f"(less where the replay cache already holds the day)")
        return

    kite = get_kite()
    if kite is None:
        print("no Kite session — cannot fetch; cached symbol-days will still replay")
    _bars_mod._MIN_INTERVAL_S = min_interval
    src = BarSource(kite=kite)
    src.resolve_tokens(symbols)
    policy = load_intraday_policy(engine="IGN")
    need_start = _date.fromisoformat(start) - timedelta(days=DAILY_LOOKBACK_DAYS)
    need_end = _date.fromisoformat(end)

    saved_engines = _detect.ENGINES
    _detect.ENGINES = [e for e in saved_engines if getattr(e, "name", "") == "IGN"]
    if not _detect.ENGINES:
        raise RuntimeError("IGN is not registered in tools.replay.detect.ENGINES")
    DATASET.parent.mkdir(parents=True, exist_ok=True)
    raw_by_symbol: dict[str, list[dict]] = {}
    t0 = time.time()
    try:
        with DATASET.open("a", encoding="utf-8") as out:
            for i, (sym, day) in enumerate(todo, 1):
                if sym not in raw_by_symbol:
                    try:
                        raw_by_symbol[sym] = daily_raw(kite, src, sym, need_start,
                                                       need_end, min_interval)
                    except Exception as e:
                        logger.warning(f"  daily fetch failed for {sym}: {e}")
                        raw_by_symbol[sym] = []
                rec = process_symbol_day(sym, day, src, raw_by_symbol[sym], policy)
                out.write(json.dumps(rec, default=str) + "\n")
                out.flush()
                if i % 50 == 0 or i == len(todo):
                    el = time.time() - t0
                    print(f"  {i}/{len(todo)}  {el / 60:.1f} min elapsed, "
                          f"~{el / i * (len(todo) - i) / 60:.0f} min left — "
                          f"{src.coverage.line()}", flush=True)
    finally:
        _detect.ENGINES = saved_engines
    print(f"done — dataset at {DATASET}")


# ── analyze ─────────────────────────────────────────────────────────────────

def load_rows(population: str) -> tuple[list[S.Row], dict[str, int]]:
    rows, status = [], {}
    if not DATASET.exists():
        return rows, status
    with DATASET.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            status[r.get("status", "?")] = status.get(r.get("status", "?"), 0) + 1
            p = r.get(population)
            if r.get("status") == "ok" and p:
                rows.append(S.Row(symbol=r["symbol"], day=r["day"], r=float(p["r"]),
                                  feats=r.get("feats") or {}, hour_bucket=p.get("hour", ""),
                                  action=p.get("action", ""),
                                  extra={"chg_pct": p.get("chg_pct"),
                                         "volume_ratio": p.get("volume_ratio")}))
    return rows, status


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                          check=False).stdout.strip()


def preflight() -> tuple[bool, str, str]:
    """(ok, reason, sha12). The holdout may only be revealed for COMMITTED,
    unmodified copies of this file and the stats file, and only once per pair."""
    files = [Path(__file__).resolve(), (HERE / "ign_feature_stats.py").resolve()]
    shas = []
    for f in files:
        rel = f.relative_to(Path(_git("rev-parse", "--show-toplevel")).resolve()).as_posix()
        if _git("status", "--porcelain", "--", str(f)):
            return False, f"{f.name} has uncommitted changes — commit it first (R2)", ""
        head = _git("rev-parse", f"HEAD:{rel}")
        disk = _git("hash-object", str(f))
        if not head or head != disk:
            return False, f"{f.name} on disk is not the committed version (R2)", ""
        shas.append(disk)
    sha12 = "".join(s[:6] for s in shas)
    if (RESULTS / f"ign_feature_holdout_{sha12}.json").exists():
        return False, ("the holdout was already revealed for these exact file versions "
                       f"(results/ign_feature_holdout_{sha12}.json) — one look only (R3)"), sha12
    return True, "", sha12


def _fmt_p(p: float) -> str:
    return "  nan" if p != p else f"{p:.3f}"


def analyze(reveal_holdout: bool) -> int:
    live, status = load_rows("live")
    ungated, _ = load_rows("ungated")
    print("=" * 92)
    print("IGN FEATURE STUDY — independent unit: first detection per (symbol, day)")
    print("=" * 92)
    total = sum(status.values())
    print(f"coverage: {total} symbol-days recorded — " +
          ", ".join(f"{k}={v}" for k, v in sorted(status.items())))
    for name, rows in (("live (first LONG at/after 10:00)", live),
                       ("ungated (first LONG any time)", ungated)):
        d = S.describe(rows)
        if d["n"]:
            print(f"  {name:<34} n={d['n']:<5} mean {d['mean_r']:+.3f}R (SE {d['se']:.3f})  "
                  f"median {d['median_r']:+.3f}R  win {d['win']:.1%}")
        else:
            print(f"  {name:<34} n=0")
    if len(live) < S.MIN_N_TOTAL:
        print(f"\nINSUFFICIENT: {len(live)} live-population symbol-days, "
              f"the pre-registered minimum is {S.MIN_N_TOTAL}. Nothing can be armed.")
        return 1

    h = S.hour_effect(ungated)
    print("\nhour effect (ungated first detection; checks migration 141's armed gate):")
    if "note" in h:
        print(f"  {h}")
    else:
        print(f"  OPEN n={h['n_open']} mean {h['open_mean_r']:+.3f}R   later n={h['n_later']} "
              f"mean {h['later_mean_r']:+.3f}R   diff {h['diff']:+.3f}R  "
              f"95% CI [{h['ci'][0]:+.3f}, {h['ci'][1]:+.3f}]")

    train, hold = S.time_split(live)
    print(f"\nsplit: train {len(train)} symbol-days "
          f"({min(r.day for r in train)}..{max(r.day for r in train)}), "
          f"holdout {len(hold)} ({min(r.day for r in hold)}..{max(r.day for r in hold)})"
          if hold else f"\nsplit: train {len(train)}, holdout 0")
    out = S.train_stage(train)
    print(f"\nTRAIN — pre-registered features (Holm alpha {S.HOLM_ALPHA}, "
          f"{len(S.HYPOTHESES)} tests):")
    print(f"  {'feature':<16}{'exp':>4}{'n':>6}{'rho':>8}{'p':>8}{'p_holm':>8}  candidate")
    for t in out["tests"]:
        exp = {1: "+", -1: "-", 0: "+/-"}[t["sign"]]
        rho = "  nan" if t["rho"] != t["rho"] else f"{t['rho']:+.3f}"
        print(f"  {t['feature']:<16}{exp:>4}{t['n']:>6}{rho:>8}{_fmt_p(t['p']):>8}"
              f"{_fmt_p(t['p_holm']):>8}  {'YES' if t['candidate'] else '-'}")
    print("\nEXPLORATORY (never arms anything):")
    for f in S.EXPLORATORY:
        t = S.test_feature(train, f, 0, with_ci=False)
        rho = "nan" if t["rho"] != t["rho"] else f"{t['rho']:+.3f}"
        print(f"  {f:<16} n={t['n']:<5} rho {rho}  p {_fmt_p(t['p'])}")

    cands = S.resolve_signs(out["tests"])
    print(f"\ncandidates carried to the holdout: {[c[0] for c in cands] or 'none'}")
    if not reveal_holdout:
        print(f"holdout NOT revealed ({len(hold)} symbol-days held back). "
              f"Commit both study files, then re-run with --reveal-holdout — once.")
        return 0
    if not cands:
        print("no candidate survived TRAIN, so there is nothing to confirm; "
              "the holdout is left unrevealed and nothing is armed.")
        return 0
    ok, why, sha12 = preflight()
    if not ok:
        print(f"REFUSED: {why}")
        return 2
    hres = S.holdout_stage(hold, cands)
    print(f"\nHOLDOUT (one look, {len(hold)} symbol-days):")
    for t in hres["tests"]:
        lo, hi = t["ci"]
        print(f"  {t['feature']:<16} n={t['n']:<5} rho {t['rho']:+.3f}  "
              f"95% CI [{lo:+.3f}, {hi:+.3f}]  "
              f"{'CONFIRMED' if t['confirmed'] else 'not confirmed'}")
    gv = S.gate_verdict(hold, hres["confirmed"])
    print(f"\nGATE on holdout: {'ARM ' + str(gv.get('features')) if gv['arm'] else 'ARM NOTHING'}"
          f" — {gv['why']}")
    if "kept_mean_r" in gv:
        print(f"  kept {gv['kept']} (mean {gv['kept_mean_r']:+.3f}R) vs dropped "
              f"{gv['dropped']} (mean {gv['dropped_mean_r']:+.3f}R); diff {gv['diff']:+.3f}R, "
              f"CI lo {gv['diff_ci_lo']:+.3f}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"ign_feature_holdout_{sha12}.json"
    path.write_text(json.dumps({"train": out, "holdout": hres, "gate": gv,
                                "n_live": len(live), "n_hold": len(hold)},
                               indent=2, default=str), encoding="utf-8")
    print(f"\nwritten {path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--min-interval", type=float, default=1.0,
                   help="seconds between Kite calls (default 1.0 — leaves headroom for "
                        "the live daemon, which shares the same rate budget)")
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("analyze")
    a.add_argument("--reveal-holdout", action="store_true")
    args = p.parse_args()
    if args.cmd == "collect":
        collect(args.start, args.end, args.min_interval, args.limit, args.dry_run)
        return 0
    return analyze(args.reveal_holdout)


if __name__ == "__main__":
    sys.exit(main())
