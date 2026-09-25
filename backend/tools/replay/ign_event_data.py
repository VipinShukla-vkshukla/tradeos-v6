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
import time
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import get_supabase
from kite.kite_client import get_kite
from tools.replay import bars as _bars_mod
from tools.replay import ign_feature_stats as S
from tools.replay import ign_feature_study as X
from tools.replay.bars import BarSource

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


def _fetch_minute(src: BarSource, sym: str, day: str) -> int:
    """One symbol-day with rate-limit resilience: the source halts after 5 consecutive
    failures, which over a multi-hour run is a transient blip, not a stop."""
    if src._consecutive_failures >= 5:
        logger.warning("  event data: pausing 60s after consecutive fetch failures")
        time.sleep(60)
        src._consecutive_failures = 0
    return len(src.get(sym, day))


def collect(min_interval: float, only: str | None = None, loose: bool = False) -> None:
    lists = freeze_lists()
    kite = get_kite()
    if kite is None:
        raise SystemExit("no Kite session")
    _bars_mod._MIN_INTERVAL_S = min_interval
    src = BarSource(kite=kite)
    src._tokens.update(INDICES)
    events = [tuple(e) for e in lists["events"]]
    loose_events = [tuple(e) for e in lists.get("events_loose", [])] if loose else []
    controls = [tuple(c) for c in lists["controls"]]
    symbols = sorted({s for s, _ in events} | {s for s, _ in controls}
                     | {s for s, _ in loose_events})
    print(f"events {len(events)}, controls {len(controls)}, symbols {len(symbols)}, "
          f"sessions {len(lists['sessions'])}", flush=True)
    src.resolve_tokens(symbols)

    if only in (None, "daily"):
        need_start = _date.fromisoformat(lists["start"]) - timedelta(days=DAILY_LOOKBACK_DAYS)
        need_end = _date.fromisoformat(lists["end"])
        t0 = time.time()
        for i, sym in enumerate(symbols, 1):
            try:
                X.daily_raw(kite, src, sym, need_start, need_end, min_interval)
            except Exception as e:
                logger.warning(f"  daily fetch failed for {sym}: {e}")
            if i % 100 == 0:
                print(f"  daily {i}/{len(symbols)}  {time.time() - t0:.0f}s", flush=True)
        for name, tok in INDICES.items():
            try:
                _daily_index(kite, name, tok, need_start, need_end, min_interval)
            except Exception as e:
                logger.warning(f"  index daily failed for {name}: {e}")

    if only in (None, "context"):
        t0 = time.time()
        done = 0
        for day in lists["sessions"]:
            for name in INDICES:
                _fetch_minute(src, name, day)
            done += 1
            if done % 50 == 0:
                print(f"  context {done}/{len(lists['sessions'])} sessions  {time.time() - t0:.0f}s  "
                      f"{src.coverage.line()}", flush=True)

    if only in (None, "minute"):
        todo = ([("control", s, d) for s, d in controls] + [("event", s, d) for s, d in events]
                + [("loose", s, d) for s, d in loose_events])
        t0 = time.time()
        for i, (kind, sym, day) in enumerate(todo, 1):
            _fetch_minute(src, sym, day)
            if i % 500 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"  minute {i}/{len(todo)}  {el / 60:.1f} min, ~{el / i * (len(todo) - i) / 60:.0f} "
                      f"min left — {src.coverage.line()}", flush=True)
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
    c.add_argument("--min-interval", type=float, default=0.5)
    c.add_argument("--only", choices=("daily", "context", "minute"), default=None)
    c.add_argument("--loose", action="store_true", help="also fetch the 2.5-3.0%% tier")
    args = p.parse_args()
    if args.cmd == "lists":
        lists = freeze_lists()
        print(f"events {len(lists['events'])} (+{len(lists['events_loose'])} loose)  controls {len(lists['controls'])} "
              f"(of which also events: {lists['controls_also_events']})  eligible {lists['n_eligible']}  "
              f"sessions {len(lists['sessions'])}  {lists['start']}..{lists['end']}")
        return 0
    collect(args.min_interval, args.only, args.loose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
