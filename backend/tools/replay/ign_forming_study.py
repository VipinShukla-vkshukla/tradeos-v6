"""
IGN forming-candle study — does the daily panel recomputed WITH today's still-forming candle
separate IGN's good LONG entries from bad, where the as-of-prior-close panel did not?

    python -m tools.replay.ign_forming_study analyze                    # TRAIN only
    python -m tools.replay.ign_forming_study analyze --reveal-holdout   # one look

No new data and no Kite calls: it re-uses the long study's dataset and its cached Kite daily
and minute bars. For each first-LONG detection it rebuilds the candle as the engine would have
held it (bars strictly before the detection instant, ltp = the last close, exactly
tools.replay.contexts.build_context) and computes intraday/trend_indicators.forming_panel.

A different quantity from the as-of panel: a +5% spike sits above its own SuperTrend by
construction, so the hypotheses are TWO-SIDED (a positive result could be mechanics rather
than trend health, and the study can only say whether it predicts R, not why).

The long study's holdout is still sealed; this study spends it, once, on hypotheses that
were never looked at on its train half. EVERYTHING BETWEEN THE MARKERS IS PRE-REGISTERED.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date as _date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import cfg
from intraday.daily_history import to_daily_bars
from intraday.trend_indicators import forming_panel
from tools.replay import ign_feature_stats as S
from tools.replay import ign_feature_study as X
from tools.replay import study_common as C
from tools.replay.bars import load_cached
from tools.replay.contexts import bars_before

# ── PRE-REGISTERED — 24-Sep-2026, before any forming-panel feature was computed ─

# All two-sided: see the module docstring.
HYPOTHESES = (
    ("above_st", 0),
    ("adx", 0),
    ("di_minus", 0),
    ("dist_sma50", 0),
    ("rsi14", 0),
    ("ret_1m", 0),
)
# Only these can become a long-gate check, and only with the sign the check acts on.
GATE_MAP = {
    "above_st": ("require_above_st", True),
    "adx": ("min_adx", 20.0),
    "di_minus": ("max_di_minus", 20.0),
}
GATE_SIGN = {"above_st": +1, "adx": +1, "di_minus": -1}
OFF_CFG = dict(require_above_st=False, min_adx=0.0, max_di_minus=0.0,
               max_prev_vol_ratio=0.0, require_sma50_gt_200=False, min_agree=0)
# Arming this would also require ign_trend_use_forming=true. Split, n floor, Holm alpha, CI
# level and the gate's size/share/CI rule are the ign_feature_stats constants, unchanged.

# ── end of pre-registration ──────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
STUDY_FILES = [Path(__file__), HERE / "study_common.py", HERE / "ign_feature_stats.py"]


def _cached_daily(symbol: str) -> list[dict] | None:
    path = X._daily_path(symbol)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            blob = json.load(fh)
        return [dict(r, date=_date.fromisoformat(r["date"])) for r in blob["rows"]]
    except Exception:
        return None


def forming_at(prior_bars, day_bars, det_ts, day: _date) -> dict | None:
    """The forming panel exactly as the engine would hold it at `det_ts`: bars STRICTLY
    before it, ltp the last close, open the first bar's open."""
    seen = bars_before(day_bars, det_ts)
    if not seen:
        return None
    p = forming_panel(prior_bars, date=day, open=seen[0].open,
                      high=max(b.high for b in seen), low=min(b.low for b in seen),
                      close=seen[-1].close, volume=sum(b.volume for b in seen))
    p["ok"] = True
    return p


def forming_rows() -> tuple[list[S.Row], dict[str, int]]:
    """One Row per long-study live-population symbol-day, features from the forming panel."""
    interval = cfg("intraday_bar_interval", "minute")
    rows: list[S.Row] = []
    n = {"records": 0, "no_daily": 0, "no_bars": 0, "as_of_not_ok": 0, "unbuildable": 0,
         "entry_mismatch": 0, "used": 0}
    daily_cache: dict[str, list[dict] | None] = {}
    if not X.DATASET.exists():
        return rows, n
    with X.DATASET.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            live = r.get("live")
            if r.get("status") != "ok" or not live:
                continue
            n["records"] += 1
            if not (r.get("feats") or {}).get("ok"):
                n["as_of_not_ok"] += 1
                continue
            sym, day = r["symbol"], r["day"]
            if sym not in daily_cache:
                daily_cache[sym] = _cached_daily(sym)
            raw = daily_cache[sym]
            if not raw:
                n["no_daily"] += 1
                continue
            day_bars = load_cached(sym, day, interval)
            if not day_bars:
                n["no_bars"] += 1
                continue
            d = _date.fromisoformat(day)
            det_ts = datetime.fromisoformat(live["ts"])
            seen = bars_before(day_bars, det_ts)
            if seen and abs(seen[-1].close - float(live["entry"])) > 1e-6:
                n["entry_mismatch"] += 1          # would mean the rebuilt candle is not the one detected on
                continue
            p = forming_at(to_daily_bars(raw, d), day_bars, det_ts, d)
            if p is None or p.get("above_st") is None:
                n["unbuildable"] += 1
                continue
            n["used"] += 1
            rows.append(S.Row(symbol=sym, day=day, r=float(live["r"]), feats=p,
                              hour_bucket=live.get("hour", ""), action=live.get("action", "")))
    return rows, n


def analyze(reveal_holdout: bool) -> int:
    rows, n = forming_rows()
    print("=" * 92)
    print("IGN FORMING-CANDLE STUDY — same detections as the long study, features with today's candle")
    print("=" * 92)
    print("coverage: " + ", ".join(f"{k}={v}" for k, v in n.items()))
    if n["entry_mismatch"]:
        print(f"WARNING: {n['entry_mismatch']} detections whose rebuilt candle does not end at the "
              f"detection price were skipped — the rebuild is not faithful for those.")
    if len(rows) < S.MIN_N_TOTAL:
        print(f"INSUFFICIENT: {len(rows)} symbol-days; minimum {S.MIN_N_TOTAL}.")
        return 1
    train, hold = S.time_split(rows)
    print(f"split: train {len(train)} ({min(r.day for r in train)}..{max(r.day for r in train)}), "
          f"holdout {len(hold)}")
    out = C.train_stage(train, HYPOTHESES)
    print(f"\nTRAIN — pre-registered forming-panel features (Holm alpha {S.HOLM_ALPHA}, "
          f"{len(HYPOTHESES)} tests, all two-sided):")
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
    ok, why, sha12 = C.preflight(STUDY_FILES, RESULTS, "ign_forming_holdout")
    if not ok:
        print(f"REFUSED: {why}")
        return 2
    hres = S.holdout_stage(hold, cands)
    print(f"\nHOLDOUT (one look, {len(hold)} symbol-days):")
    for t in hres["tests"]:
        lo, hi = t["ci"]
        print(f"  {t['feature']:<16} n={t['n']:<5} rho {t['rho']:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  "
              f"{'CONFIRMED' if t['confirmed'] else 'not confirmed'}")
    from intraday.ign_trend import trend_verdict
    can = C.armable(hres["tests"], GATE_SIGN)
    gv = C.gate_verdict(hold, can, gate_map=GATE_MAP, off_cfg=OFF_CFG, verdict_fn=trend_verdict)
    print(f"\nFORMING GATE on holdout: {'ARM ' + str(gv.get('features')) if gv['arm'] else 'ARM NOTHING'}"
          f" — {gv['why']}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"ign_forming_holdout_{sha12}.json"
    path.write_text(json.dumps({"train": out, "holdout": hres, "gate": gv, "n": len(rows),
                                "n_hold": len(hold)}, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten {path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("--reveal-holdout", action="store_true")
    args = p.parse_args()
    return analyze(args.reveal_holdout)


if __name__ == "__main__":
    sys.exit(main())
