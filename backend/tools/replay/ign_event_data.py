"""
Kite data for the IGN event study — every ignition-like symbol-day, a uniform random control
sample, and market context, all cached and resumable.

    python -m tools.replay.ign_event_data lists                   # build + freeze the symbol-day lists
    python -m tools.replay.ign_event_data collect                 # fetch everything (slow, resumable)
    python -m tools.replay.ign_event_data collect --min-interval 1.0

WHY A WIDER SET THAN IGN'S OWN DETECTIONS. The earlier studies replayed IGN's live rules, so they
only ever saw days those rules fire on. To learn what separates a genuine ignition from a false
one, and to test any redesign, the study needs the whole population of ignition-like days at a
LOOSER threshold than live (so thresholds can be studied, not assumed), a control sample of
ordinary days (so "does ignition add anything over being long a random liquid stock" has a
benchmark), and the market around each one (Nifty 50, Nifty 500, India VIX).

    events    price_history_yf: day's high >= +3.0% vs prior close (a "loose" tier, 2.5-3.0%, is
              listed separately and fetched last), prior-day turnover >= Rs 25 Cr,
              prior close >= Rs 50. NO filter on the day's volume: total volume is only known at
              the close, so conditioning on it would silently drop the ignitions whose volume
              faded — exactly the failures — and make every event look better than it was. Volume
              is a bar-level condition using data up to the trigger only. price_history_yf is
              dividend-adjusted, so it only NAMES the days; no decision input comes from it.
    controls  a seeded uniform sample of liquid symbol-days (movers included), FROZEN in the lists
              file so re-runs never resample.
    context   minute bars for NIFTY 50, NIFTY 500, INDIA VIX for every session; their daily bars.

Every price the study uses comes from Kite (minute and daily). Minute bars go through the same
gz cache as every replay tool (tools/replay/bars.py); daily bars through ign_feature_study.daily_raw.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import get_supabase
from kite.kite_client import get_kite
from tools.replay import bars as _bars_mod
from tools.replay import ign_feature_stats as S
from tools.replay import ign_feature_study as X
from tools.replay.bars import Bar, BarSource

PERIOD_START = "2025-01-02"
UP_PCT = 3.0            # primary tier: the design region for IGN (live trigger is 3.5%)
LOOSE_UP_PCT = 2.5      # loose tier, fetched last and only on request
VOL_MULT = 0.0     # NO day-volume filter: the day's total volume is only known at the close
N_CONTROLS = 3000
CONTROL_SEED = 20260925
INDICES = {"NIFTY 50": 256265, "NIFTY 500": 268041, "INDIA VIX": 264969}
DAILY_LOOKBACK_DAYS = 800          # ~ 3 years, so a 200-day average exists from the first event

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
LISTS = CACHE / "ign_event_lists.json"


def build_lists(sb, start: str, end: str) -> dict:
    """The frozen symbol-day lists. `events` is a loose superset of what IGN can fire on."""
    daily = X.load_price_history(sb, start, end)
    events = S.candidate_days(daily, start, end, up_pct=UP_PCT, vol_mult=VOL_MULT)
    ev = set(events)
    loose = [c for c in S.candidate_days(daily, start, end, up_pct=LOOSE_UP_PCT, vol_mult=VOL_MULT)
             if c not in ev]
    eligible = []
    for sym, rows in daily.items():
        for i in range(1, len(rows)):
            d = str(rows[i]["date"])[:10]
            if d < start or d > end:
                continue
            pc, pv = float(rows[i - 1]["close"] or 0), float(rows[i - 1]["volume"] or 0)
            if pc >= S.MIN_PRICE and pv > 0 and pc * pv / 1e7 >= S.MIN_TURNOVER_CR:
                eligible.append((sym, d))
    rng = random.Random(CONTROL_SEED)
    controls = sorted(rng.sample(eligible, min(N_CONTROLS, len(eligible))))
    sessions = sorted({str(r["date"])[:10] for rows in daily.values() for r in rows
                       if start <= str(r["date"])[:10] <= end})
    return {"start": start, "end": end, "up_pct": UP_PCT, "vol_mult": VOL_MULT,
            "n_eligible": len(eligible), "events": events, "events_loose": loose,
            "controls": controls,
            "controls_also_events": sum(1 for c in controls if c in ev),
            "sessions": sessions}


def freeze_lists(end: str | None = None) -> dict:
    if LISTS.exists():
        return json.loads(LISTS.read_text(encoding="utf-8"))
    end = end or (_date.today() - timedelta(days=1)).isoformat()
    lists = build_lists(get_supabase(), PERIOD_START, end)
    LISTS.parent.mkdir(parents=True, exist_ok=True)
    LISTS.write_text(json.dumps(lists), encoding="utf-8")
    return lists


MAX_SPAN_DAYS = 58          # Kite allows 60 calendar days of minute bars per request
MIN_DAYS_FOR_RANGE = 2      # a lone day is cheaper as a one-day request than a 58-day one


def plan_windows(days: list[str], max_span: int = MAX_SPAN_DAYS) -> list[list[str]]:
    """Group sorted ISO dates into windows each spanning <= max_span calendar days, so one
    request can cover every needed day inside it. Pure, so it can be tested offline."""
    out: list[list[str]] = []
    for d in sorted(set(days)):
        if out and (_date.fromisoformat(d) - _date.fromisoformat(out[-1][0])).days <= max_span:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def split_by_day(raw: list[dict], wanted: set[str]) -> dict[str, list[Bar]]:
    """Kite's multi-day response -> {day: [Bar]} for the wanted days only, in the exact shape
    BarSource.get stores for a one-day request."""
    from config import IST
    out: dict[str, list[Bar]] = {}
    for b in raw:
        ts = b["date"]
        if ts.tzinfo is None:
            ts = IST.localize(ts)
        day = ts.date().isoformat()
        if day not in wanted:
            continue
        out.setdefault(day, []).append(Bar(
            ts=ts, open=float(b["open"]), high=float(b["high"]), low=float(b["low"]),
            close=float(b["close"]), volume=float(b.get("volume") or 0)))
    return out


class _Limiter:
    """One shared request clock, so N worker threads together stay under Kite's ~3 req/s."""

    def __init__(self, min_interval: float):
        self.min_interval, self._last, self._lock = min_interval, 0.0, threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


def _missing(symbol: str, days) -> list[str]:
    return sorted(d for d in set(days) if not _bars_mod._cache_path(symbol, d, "minute").exists())


def _fetch_window(kite, limiter: _Limiter, token: int, symbol: str, window: list[str]) -> tuple[int, int]:
    """One range request for a window; stores each needed day. Returns (stored, missed)."""
    lo, hi = _date.fromisoformat(window[0]), _date.fromisoformat(window[-1])
    raw = None
    for attempt in range(5):
        limiter.wait()
        try:
            raw = kite.historical_data(token, lo, hi, "minute") or []
            break
        except Exception as e:
            if "Too many" in str(e) or "429" in str(e) or "timed out" in str(e).lower():
                time.sleep(3 * (attempt + 1))
                continue
            logger.warning(f"  {symbol} {window[0]}..{window[-1]}: {type(e).__name__}: {str(e)[:80]}")
            break
    if raw is None:
        return 0, len(window)
    got = split_by_day(raw, set(window))
    stored = 0
    for day, bars in got.items():
        _bars_mod._store(symbol, day, "minute", bars)
        _bars_mod._write_manifest({"symbol": symbol, "date": day, "interval": "minute",
                                   "rows": len(bars), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "status": "OK"})
        stored += 1
    return stored, len(window) - stored


def fetch_minute_days(kite, tokens: dict[str, int], wanted: dict[str, list[str]],
                      min_interval: float = 0.4, workers: int = 3, label: str = "minute") -> None:
    """Fill the minute-bar cache for {symbol: [days]}, windowed and threaded, resumable."""
    jobs: list[tuple[str, list[str]]] = []
    for sym, days in wanted.items():
        need = _missing(sym, days)
        if not need or sym not in tokens:
            continue
        for w in plan_windows(need):
            jobs.append((sym, w))
    total_days = sum(len(w) for _, w in jobs)
    print(f"  {label}: {len(jobs)} requests for {total_days} uncached symbol-days", flush=True)
    limiter, lock = _Limiter(min_interval), threading.Lock()
    prog = {"jobs": 0, "stored": 0, "missed": 0}
    t0 = time.time()

    def run(job):
        sym, w = job
        st, ms = _fetch_window(kite, limiter, tokens[sym], sym, w)
        with lock:
            prog["jobs"] += 1
            prog["stored"] += st
            prog["missed"] += ms
            if prog["jobs"] % 100 == 0 or prog["jobs"] == len(jobs):
                el = time.time() - t0
                print(f"  {label} {prog['jobs']}/{len(jobs)} requests, {prog['stored']} days stored, "
                      f"{prog['missed']} missing, {el / 60:.1f} min, ~{el / prog['jobs'] * (len(jobs) - prog['jobs']) / 60:.0f} min left",
                      flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, jobs))


def collect(min_interval: float, only: str | None = None, loose: bool = False, workers: int = 3) -> None:
    lists = freeze_lists()
    kite = get_kite()
    if kite is None:
        raise SystemExit("no Kite session")
    src = BarSource(kite=kite)
    src._tokens.update(INDICES)
    events = [tuple(e) for e in lists["events"]]
    loose_events = [tuple(e) for e in lists.get("events_loose", [])] if loose else []
    controls = [tuple(c) for c in lists["controls"]]
    symbols = sorted({s for s, _ in events} | {s for s, _ in controls}
                     | {s for s, _ in loose_events})
    print(f"events {len(events)}, controls {len(controls)}, symbols {len(symbols)}, "
          f"sessions {len(lists['sessions'])}", flush=True)
    tokens = dict(src.resolve_tokens(symbols))
    tokens.update(INDICES)

    if only in (None, "daily"):
        need_start = _date.fromisoformat(lists["start"]) - timedelta(days=DAILY_LOOKBACK_DAYS)
        need_end = _date.fromisoformat(lists["end"])
        t0 = time.time()
        for i, sym in enumerate(symbols, 1):
            try:
                X.daily_raw(kite, src, sym, need_start, need_end, max(min_interval, 0.5))
            except Exception as e:
                logger.warning(f"  daily fetch failed for {sym}: {e}")
            if i % 100 == 0:
                print(f"  daily {i}/{len(symbols)}  {time.time() - t0:.0f}s", flush=True)
        for name, tok in INDICES.items():
            try:
                _daily_index(kite, name, tok, need_start, need_end, max(min_interval, 0.5))
            except Exception as e:
                logger.warning(f"  index daily failed for {name}: {e}")

    if only in (None, "context"):
        fetch_minute_days(kite, tokens, {n: lists["sessions"] for n in INDICES},
                          min_interval, workers, "context")

    if only in (None, "minute"):
        wanted: dict[str, list[str]] = {}
        for sym, day in controls + events + loose_events:
            wanted.setdefault(sym, []).append(day)
        fetch_minute_days(kite, tokens, wanted, min_interval, workers, "minute")
    print("done", flush=True)


def _daily_index(kite, name: str, token: int, start: _date, end: _date, spacing: float) -> None:
    import gzip
    from intraday.daily_history import fetch_daily
    path = X.DAILY_DIR / f"__{name.replace(' ', '_')}.json.gz"
    if path.exists():
        try:
            blob = json.loads(gzip.open(path, "rt", encoding="utf-8").read())
            if _date.fromisoformat(blob["start"]) <= start and _date.fromisoformat(blob["end"]) >= end:
                return
        except Exception:
            pass
    raw = fetch_daily(kite, token, start, end, spacing_s=spacing)
    rows = []
    for r in raw:
        d = r["date"]
        d = d.date() if hasattr(d, "date") and callable(d.date) else d
        rows.append({"date": d.isoformat(), "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(r["close"]),
                     "volume": float(r.get("volume") or 0)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"start": start.isoformat(), "end": end.isoformat(), "rows": rows}, fh)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lists")
    c = sub.add_parser("collect")
    c.add_argument("--min-interval", type=float, default=0.4,
                   help="seconds between request STARTS across all workers (3 req/s is Kite's cap)")
    c.add_argument("--workers", type=int, default=3)
    c.add_argument("--only", choices=("daily", "context", "minute"), default=None)
    c.add_argument("--loose", action="store_true", help="also fetch the 2.5-3.0%% tier")
    args = p.parse_args()
    if args.cmd == "lists":
        lists = freeze_lists()
        print(f"events {len(lists['events'])} (+{len(lists['events_loose'])} loose)  controls {len(lists['controls'])} "
              f"(of which also events: {lists['controls_also_events']})  eligible {lists['n_eligible']}  "
              f"sessions {len(lists['sessions'])}  {lists['start']}..{lists['end']}")
        return 0
    collect(args.min_interval, args.only, args.loose, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
