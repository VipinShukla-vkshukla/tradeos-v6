"""
IGN SHORT-side daily-trend study — does any daily signal separate IGN's good SHORTs from bad?

    python -m tools.replay.ign_short_study collect --start 2026-03-02 --end 2026-09-23
    python -m tools.replay.ign_short_study analyze                    # TRAIN only
    python -m tools.replay.ign_short_study analyze --reveal-holdout   # one look

The mirror of ign_feature_study.py (which it imports and does not modify): IGN's live SHORT
rules replayed over real Kite minute bars for every liquid symbol-day that fell >= 3%, first
SHORT detection per (symbol, day), live exit ladder, gross R, features as of the prior
completed session. Same unit, statistics, Holm correction and sealed time-ordered holdout.

Shorts are rare (IGN's own can_short() refuses a name already down > 6%, and it needs >= 3.5%),
so the pre-registered minimum n applies: fewer than S.MIN_N_TOTAL symbol-days and nothing can
be armed, however good the survivors look.

EVERYTHING BETWEEN THE MARKERS BELOW IS PRE-REGISTERED and committed before collection.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import fetch_all, get_supabase
from intraday.daily_history import build_panel, to_daily_bars
from intraday.exit_policy import load_intraday_policy
from kite.kite_client import get_kite
from tools.replay import bars as _bars_mod
from tools.replay import detect as _detect
from tools.replay import ign_feature_stats as S
from tools.replay import ign_feature_study as X
from tools.replay import study_common as C
from tools.replay.bars import BarSource

# ── PRE-REGISTERED — 24-Sep-2026, before any short-side replay result existed ─

# (feature, expected sign of Spearman(feature, gross R) for a SHORT; 0 = two-sided).
HYPOTHESES = (
    ("above_st", -1),         # price BELOW its daily SuperTrend line helps a short
    ("adx", +1),              # a trending market carries a move, either way
    ("di_minus", +1),         # selling pressure under the spike
    ("prev_vol_ratio", -1),   # yesterday already a blow-off -> exhausted
    ("sma50_gt_200", 0),
    ("dist_sma50", 0),
    ("rsi14", 0),
)
# feature -> (short-gate parameter, fixed value). Round numbers, not tuned on the data.
GATE_MAP = {
    "above_st": ("require_below_st", True),
    "adx": ("min_adx", 20.0),
    "di_minus": ("min_di_minus", 20.0),
    "prev_vol_ratio": ("max_prev_vol_ratio", 2.0),
    "sma50_gt_200": ("require_sma50_lt_200", True),
}
# The sign a feature's rho must have for its gate check to be the one that helps.
GATE_SIGN = {"above_st": -1, "adx": +1, "di_minus": +1, "prev_vol_ratio": -1,
             "sma50_gt_200": -1}
OFF_CFG = dict(require_below_st=False, min_adx=0.0, min_di_minus=0.0,
               max_prev_vol_ratio=0.0, require_sma50_lt_200=False, min_agree=0)
DOWN_PCT = 3.0                  # loose prefilter: day's low vs prior close
# Thresholds, split, n floor, Holm alpha, CI level and the gate's size/share/CI rule are the
# ign_feature_stats constants, imported unchanged.

# ── end of pre-registration ──────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
DATASET = X.CACHE / "ign_short_dataset.jsonl"
RESULTS = HERE / "results"
STUDY_FILES = [Path(__file__), HERE / "study_common.py", HERE / "ign_feature_stats.py"]


def short_candidate_days(daily: dict[str, list[dict]], start: str, end: str, *,
                         min_price: float = S.MIN_PRICE,
                         min_turnover_cr: float = S.MIN_TURNOVER_CR,
                         vol_mult: float = S.PREFILTER_VOL_MULT,
                         down_pct: float = DOWN_PCT) -> list[tuple[str, str]]:
    """(symbol, day) worth fetching for a SHORT: a loose superset (the rules decide)."""
    out = []
    for sym, rows in daily.items():
        for i in range(1, len(rows)):
            d = str(rows[i]["date"])[:10]
            if d < start or d > end:
                continue
            pc, pv = float(rows[i - 1]["close"] or 0), float(rows[i - 1]["volume"] or 0)
            if pc < min_price or pv <= 0 or pc * pv / 1e7 < min_turnover_cr:
                continue
            lo, vol = float(rows[i]["low"] or 0), float(rows[i]["volume"] or 0)
            if lo <= 0 or lo / pc - 1.0 > -down_pct / 100.0 or vol < vol_mult * pv:
                continue
            out.append((sym, d))
    return sorted(out, key=lambda t: (t[1], t[0]))


def first_short(dets: list, population: str):
    """First SHORT detection of a symbol-day: 'live' = at/after 10:00 (what the armed
    open-hour gate lets through), 'ungated' = any time."""
    from datetime import time as dtime
    for d in sorted(dets, key=lambda d: d.ts):
        if d.direction != "SHORT":
            continue
        if population == "live" and d.ts.time() < dtime(10, 0):
            continue
        return d
    return None


def load_price_history_lows(sb, start: str, end: str) -> dict[str, list[dict]]:
    lo = (_date.fromisoformat(start) - timedelta(days=45)).isoformat()
    rows = fetch_all(
        lambda: sb.table("price_history_yf").select("symbol,date,low,close,volume")
                  .gte("date", lo).lte("date", end),
        order_by="symbol,date")
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(r)
    return out


def process_symbol_day(sym: str, day: str, src: BarSource, raw: list[dict],
                       policy: dict) -> dict:
    """Never raises, never returns nothing: every symbol-day leaves a status."""
    rec: dict = {"symbol": sym, "day": day}
    day_bars = src.get(sym, day)
    if not day_bars:
        return dict(rec, status="no_bars")
    d = _date.fromisoformat(day)
    prev = S.prev_from_daily(to_daily_bars(raw, d), d)
    if prev is None:
        return dict(rec, status="no_prev")
    if not S.gap_ok(prev["close"], day_bars[0].open):
        return dict(rec, status="gap_guard")
    with X._cfg_override({"ign_exclude_open_hour_enabled": "false",
                          "ign_trend_gate_enabled": "false",
                          "ign_trend_short_gate_enabled": "false"}):
        dets = _detect.replay_symbol_day(sym, day, day_bars, prev=prev, dedup_pct=-1.0)
    dets = [x for x in dets if x.engine == "IGN"]
    rec["feats"] = build_panel(raw, d)
    rec["n_dets"] = len(dets)
    if not any(x.direction == "SHORT" for x in dets):
        return dict(rec, status="no_short_detection")
    rec["status"] = "ok"
    for pop in ("live", "ungated"):
        det = first_short(dets, pop)
        walked = X._walk_detection(det, day_bars, policy) if det is not None else None
        if walked is None:
            continue
        r, action = walked
        rec[pop] = {"ts": det.ts.isoformat(), "entry": det.entry, "stop": det.stop,
                    "target": det.target, "r": round(r, 4), "action": action,
                    "hour": S.hour_bucket_of(det.ts), "chg_pct": det.meta.get("chg_pct"),
                    "volume_ratio": det.meta.get("volume_ratio")}
    return rec


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
    cands = short_candidate_days(load_price_history_lows(sb, start, end), start, end)
    if limit:
        cands = cands[:limit]
    done = _done_keys()
    todo = [c for c in cands if c not in done]
    symbols = sorted({s for s, _ in todo})
    print(f"short candidates {len(cands)} symbol-days ({len(symbols)} symbols) in {start}..{end}; "
          f"already collected {len(cands) - len(todo)}; to fetch {len(todo)}")
    if dry_run:
        print(f"dry run — ~{len(todo) * min_interval / 60:.0f} min of minute-bar fetching at "
              f"{min_interval}s/call, less where the replay cache already holds the day")
        return
    kite = get_kite()
    _bars_mod._MIN_INTERVAL_S = min_interval
    src = BarSource(kite=kite)
    src.resolve_tokens(symbols)
    policy = load_intraday_policy(engine="IGN")
    need_start = _date.fromisoformat(start) - timedelta(days=X.DAILY_LOOKBACK_DAYS)
    need_end = _date.fromisoformat(end)
    saved = _detect.ENGINES
    _detect.ENGINES = [e for e in saved if getattr(e, "name", "") == "IGN"]
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
                        raw_by_symbol[sym] = X.daily_raw(kite, src, sym, need_start, need_end,
                                                         min_interval)
                    except Exception as e:
                        logger.warning(f"  daily fetch failed for {sym}: {e}")
                        raw_by_symbol[sym] = []
                rec = process_symbol_day(sym, day, src, raw_by_symbol[sym], policy)
                out.write(json.dumps(rec, default=str) + "\n")
                out.flush()
                if i % 50 == 0 or i == len(todo):
                    el = time.time() - t0
                    print(f"  {i}/{len(todo)}  {el / 60:.1f} min, ~{el / i * (len(todo) - i) / 60:.0f} "
                          f"min left — {src.coverage.line()}", flush=True)
    finally:
        _detect.ENGINES = saved
    print(f"done — dataset at {DATASET}")


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
            st = r.get("status", "?")
            status[st] = status.get(st, 0) + 1
            p = r.get(population)
            if st == "ok" and p:
                rows.append(S.Row(symbol=r["symbol"], day=r["day"], r=float(p["r"]),
                                  feats=r.get("feats") or {}, hour_bucket=p.get("hour", ""),
                                  action=p.get("action", "")))
    return rows, status


def analyze(reveal_holdout: bool) -> int:
    live, status = load_rows("live")
    ung, _ = load_rows("ungated")
    print("=" * 92)
    print("IGN SHORT STUDY — independent unit: first SHORT detection per (symbol, day)")
    print("=" * 92)
    print(f"coverage: {sum(status.values())} symbol-days — " +
          ", ".join(f"{k}={v}" for k, v in sorted(status.items())))
    for name, rows in (("live (first SHORT at/after 10:00)", live), ("ungated", ung)):
        d = S.describe(rows)
        print(f"  {name:<36} n={d['n']:<5}" + (
            f" mean {d['mean_r']:+.3f}R (SE {d['se']:.3f})  median {d['median_r']:+.3f}R  "
            f"win {d['win']:.1%}" if d["n"] else ""))
    if len(live) < S.MIN_N_TOTAL:
        print(f"\nINSUFFICIENT: {len(live)} live-population symbol-days; the pre-registered "
              f"minimum is {S.MIN_N_TOTAL}. Nothing can be armed on the short side.")
        return 1
    train, hold = S.time_split(live)
    print(f"\nsplit: train {len(train)} ({min(r.day for r in train)}..{max(r.day for r in train)}), "
          f"holdout {len(hold)}")
    out = C.train_stage(train, HYPOTHESES)
    print(f"\nTRAIN — pre-registered features (Holm alpha {S.HOLM_ALPHA}, {len(HYPOTHESES)} tests):")
    C.print_train(out)
    cands = S.resolve_signs(out["tests"])
    print(f"\ncandidates carried to the holdout: {[c[0] for c in cands] or 'none'}")
    if not reveal_holdout:
        print(f"holdout NOT revealed ({len(hold)} held back). Commit the study files, then "
              f"re-run with --reveal-holdout — once.")
        return 0
    if not cands:
        print("no candidate survived TRAIN; the holdout stays unrevealed and nothing is armed.")
        return 0
    ok, why, sha12 = C.preflight(STUDY_FILES, RESULTS, "ign_short_holdout")
    if not ok:
        print(f"REFUSED: {why}")
        return 2
    hres = S.holdout_stage(hold, cands)
    print(f"\nHOLDOUT (one look, {len(hold)} symbol-days):")
    for t in hres["tests"]:
        lo, hi = t["ci"]
        print(f"  {t['feature']:<16} n={t['n']:<5} rho {t['rho']:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  "
              f"{'CONFIRMED' if t['confirmed'] else 'not confirmed'}")
    from intraday.ign_trend import trend_verdict_short
    can = C.armable(hres["tests"], GATE_SIGN)
    gv = C.gate_verdict(hold, can, gate_map=GATE_MAP, off_cfg=OFF_CFG,
                        verdict_fn=trend_verdict_short)
    print(f"\nSHORT GATE on holdout: {'ARM ' + str(gv.get('features')) if gv['arm'] else 'ARM NOTHING'}"
          f" — {gv['why']}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"ign_short_holdout_{sha12}.json"
    path.write_text(json.dumps({"train": out, "holdout": hres, "gate": gv, "n_live": len(live),
                                "n_hold": len(hold)}, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten {path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--min-interval", type=float, default=1.0)
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
