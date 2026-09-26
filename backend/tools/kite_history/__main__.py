"""Command line for the Kite history folder. See download.py for what each command does."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.kite_history import download as D
from tools.kite_history import state, store, universe, verify

README = """# Kite historical data

Written by `python -m tools.kite_history` (TradeOS backend). Everything here came from Kite's historical API.

    instruments/all_<date>.parquet          the full instrument master on that day
    minute/<SEGMENT>/<SYMBOL>/<YYYY>.parquet   one-minute candles, one file per symbol per year
    day/<SEGMENT>/<SYMBOL>.parquet             daily candles, one file per symbol
    _state/progress.db, download.log          bookkeeping

SEGMENT is NSE (equities and ETFs), INDICES (NSE indices) or NFO-FUT (futures, with an `oi` column).

Columns: ts (Asia/Kolkata, the START of the candle; 09:15 is the first minute), open, high, low, close (float64),
volume (int64), oi (int64, derivatives only).

Load one symbol:

    from tools.kite_history.store import read
    df = read(r"{root}", "minute", "NSE", "RELIANCE", start="2024-01-01", end="2024-03-31")

or straight with pandas / DuckDB: `pd.read_parquet(r"{root}\\minute\\NSE\\RELIANCE")` reads every year at once.

## Read before training on it

* Kite BACK-ADJUSTS its history for splits and bonuses as of the day you download (RELIANCE's 2015 prices are
  on today's post-bonus scale, and volumes are scaled to match), so a stored price is not what traded that day.
  It does not adjust for everything: demergers and some other events remain as jumps, and a few bars are bad
  (zero prices, one-day spikes). `verify` lists >25% one-day close moves and zero-price bars; exclude or fix
  those before using a long series.
* The adjustment is retroactive, so two downloads made either side of a corporate action are on different
  scales. `update` compares the days it re-fetches with what is stored and, if Kite has re-adjusted, downloads
  that symbol's whole history again and replaces it (the old files are kept until the new history has arrived
  and reaches back as far). Never append candles from a different download date by hand.
* Kite serves candles only. There is no tick, market-depth or order-book history, so one minute is the
  finest resolution that exists. Other intervals (5, 15, 60 minute) can be built from these minutes.
* A stock that did not trade in a minute has no candle for it: gaps are real, not missing data.
* Individual expired futures contracts and options are not available (Kite only serves history for instruments
  still in its instrument master). A rolled "continuous" daily futures series does exist; it is not downloaded here.
* `python -m tools.kite_history update` brings it forward; `verify` checks integrity; `status` shows coverage.
"""


def keep_awake(on: bool = True) -> bool:
    """Ask Windows not to idle-sleep while this process runs (per process, released when it exits; no system
    setting is changed). A closed lid or an explicit Sleep still sleeps the machine. False if unsupported."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def _kite():
    from kite.kite_client import get_kite
    k = get_kite()
    if k is None:
        print("No valid Kite access token. Refresh it (about 15 seconds):\n"
              "  cd backend && python -m kite.token_manager --login-url      (open it, log in)\n"
              "  cd backend && python -m kite.token_manager --exchange <request_token from the redirect URL>")
        raise SystemExit(2)
    return k


def _targets(kite, root: Path, args) -> list:
    rows = kite.instruments() or []
    day = datetime.now().date().isoformat()
    path = universe.save_instruments(root, rows, day)
    print(f"instrument master: {len(rows)} rows -> {path.name}")
    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    return universe.build_targets(rows, segs, universe.latest_turnover(), only, args.limit)


def cmd_universe(args) -> int:
    kite = _kite()
    root = Path(args.root)
    t = _targets(kite, root, args)
    from collections import Counter
    print("targets:", dict(Counter(x.segment for x in t)), f"= {len(t)} instruments")
    return 0


def cmd_probe(args) -> int:
    kite = _kite()
    rows = kite.instruments("NSE") or []
    tok = next((r["instrument_token"] for r in rows if r.get("tradingsymbol") == args.symbol and r.get("segment") == "NSE"), None)
    if tok is None:
        print(f"{args.symbol} not found in the NSE instrument dump")
        return 1
    print(f"{args.symbol} token {tok}: a 10-day window in mid-January of each year, per interval")
    for interval in ("minute", "day"):
        print(f"\n  interval {interval}")
        for year in range(date.today().year - 1, 2004, -1):
            s, e = date(year, 1, 5), date(year, 1, 16)
            t0 = time.time()
            try:
                got = kite.historical_data(tok, s, e, interval)
                print(f"    {year}: {len(got):5d} candles  ({time.time() - t0:.2f}s)")
            except Exception as ex:
                print(f"    {year}: ERROR {type(ex).__name__}: {str(ex)[:80]}")
            time.sleep(0.4)
    return 0


def _run(args, mode: str) -> int:
    kite = _kite()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(README.format(root=str(root)), encoding="utf-8")
    from loguru import logger
    logger.add(root / "_state" / "download.log", level="INFO", rotation="20 MB")
    targets = _targets(kite, root, args)
    since = date.fromisoformat(args.since) if args.since else None
    print(f"keep-awake requested: {keep_awake(True)}", flush=True)
    res = D.run(D.FastKite(kite), root, targets, args.interval, mode=mode, workers=args.workers, min_interval=args.min_interval,
                since=since, allow_market_hours=args.allow_market_hours, redo=args.redo,
                log=lambda m: (print(m, flush=True), logger.info(m)))
    keep_awake(False)
    print("\n" + res.line())
    return 3 if res.token_expired else 0


def cmd_status(args) -> int:
    root = Path(args.root)
    db = root / "_state" / "progress.db"
    if not db.exists():
        print(f"nothing downloaded yet under {root}")
        return 0
    prog = state.Progress(db)
    rows = prog.summary()
    print(f"{'interval':10s}{'segment':10s}{'status':9s}{'symbols':>9s}{'rows':>15s}{'requests':>10s}")
    for (iv, seg, st), v in rows.items():
        print(f"{iv:10s}{seg:10s}{st:9s}{v['symbols']:9d}{v['rows']:15,d}{v['requests']:10d}")
    size = sum(p.stat().st_size for p in root.rglob("*.parquet")) / 1e9
    print(f"\n{size:.2f} GB of Parquet under {root}")
    for iv in ("minute", "day"):
        for status in (state.ERROR, state.SKIPPED):
            lst = prog.with_status(iv, status)
            if lst:
                print(f"{iv} {status}: {len(lst)}  e.g. {', '.join(s for _, s in lst[:6])}")
    prog.close()
    return 0


def cmd_report(args) -> int:
    from tools.kite_history import report
    root = Path(args.root)
    if not (root / "_state" / "progress.db").exists():
        print(f"nothing downloaded yet under {root}")
        return 0
    report.print_report(report.build(root, args.interval, args.integrity))
    return 0


def cmd_verify(args) -> int:
    root = Path(args.root)
    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    res = verify.verify_all(root, args.interval, segs, args.sample)
    hard = {k: v for k, v in res.items() if verify.has_hard_errors(v)}
    print(f"checked {len(res)} symbols; {len(hard)} with hard errors")
    for (seg, sym), r in list(hard.items())[:20]:
        print("  ", seg, sym, {k: r[k] for k in verify.HARD if r[k]})
    info = [(seg, sym, r) for (seg, sym), r in res.items() if r["big_day_jumps"]]
    print(f"{len(info)} symbols have a >25% one-day close jump (unadjusted corporate action or a real limit move); "
          f"first few: " + "; ".join(f"{s} {r['big_day_jumps'][:3]}" for _, s, r in info[:5]))
    return 1 if hard else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="tools.kite_history", description=__doc__)
    p.add_argument("--root", default=str(store.DEFAULT_ROOT), help="data folder (default: $KITE_HISTORY_DIR or D:\\kite_history)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, targets=True):
        sp.add_argument("--interval", default="minute", choices=sorted(D.MAX_SPAN_DAYS))
        if targets:
            sp.add_argument("--segments", default=",".join(universe.DEFAULT_SEGMENTS))
            sp.add_argument("--only", default=None, help="comma-separated tradingsymbols")
            sp.add_argument("--limit", type=int, default=None)

    sp = sub.add_parser("universe"); common(sp)
    sp = sub.add_parser("probe"); sp.add_argument("--symbol", default="RELIANCE")
    for name in ("backfill", "update"):
        sp = sub.add_parser(name); common(sp)
        sp.add_argument("--workers", type=int, default=3)
        sp.add_argument("--min-interval", type=float, default=0.4)
        sp.add_argument("--since", default=None, help="do not look further back than this date")
        sp.add_argument("--redo", action="store_true", help="refetch symbols already finished")
        sp.add_argument("--allow-market-hours", action="store_true")
    sub.add_parser("status")
    sp = sub.add_parser("report"); common(sp, targets=False)
    sp.add_argument("--integrity", action="store_true", help="also run the full integrity pass over every stored symbol (slow)")
    sp = sub.add_parser("verify"); common(sp, targets=False)
    sp.add_argument("--segments", default=",".join(universe.DEFAULT_SEGMENTS)); sp.add_argument("--sample", type=int, default=None)

    args = p.parse_args()
    return {"universe": cmd_universe, "probe": cmd_probe, "status": cmd_status, "verify": cmd_verify, "report": cmd_report,
            "backfill": lambda a: _run(a, "backfill"), "update": lambda a: _run(a, "update")}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
