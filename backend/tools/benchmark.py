"""
A comparable, book-by-book snapshot of what TradeOS is actually doing.
READ-ONLY.

    python -m tools.benchmark snapshot --label "before floor fix"
    python -m tools.benchmark snapshot --label "after floor fix"
    python -m tools.benchmark compare benchmarks/<file1>.json benchmarks/<file2>.json

WHY THIS EXISTS — 10-Aug-2026
------------------------------
The operator asked directly: "how can I test my entire TradeOS framework even
after a single code change... so I gain confidence that what I am doing is
correct and not breaking anything." `tools.regression_check` answers the LOGIC
half of that (do the offline checks pass, does the allocator replay run clean)
but says nothing about the actual STATE of the live book — how many positions
are open, what the real win rate has been, which engines are pulling their
weight. This tool captures that state as a single comparable snapshot, so
"before this change" and "after this change" are two files that can be
diffed line by line instead of two runs held in memory and compared by eye.

WHAT IT DOES NOT DO
--------------------
It does not run tools.verify or the replay tools — run
`tools.regression_check` for that, first, always. This tool answers a
DIFFERENT question: not "is the logic still correct" but "what does the real
book actually look like right now." A snapshot is only meaningful taken
AFTER regression_check passes clean.

It is not a backtest. Every number here is CURRENT STATE — open positions,
recent closed trades, current engine standings — not a simulation of what a
policy change would have done historically. Use tools.allocator_replay /
tools.exit_ladder_replay for that question.

GROUND TRUTH ONLY. Closed-trade stats here come straight from
`closed_positions.r_multiple` — the same column the operator's own dashboard
reads — not from any model-derived or recomputed figure. This project spent
today discovering exactly how easy it is for a "derived" number to disagree
with the real ledger; this tool deliberately stays on the ledger side of that
line.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, today_ist

BENCH_DIR = Path(__file__).parent.parent / "benchmarks"


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=5,
                            cwd=str(Path(__file__).parent.parent))
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _closed_trade_stats(sb, framework: str, days: int) -> dict:
    """Ground-truth win rate / R / P&L from closed_positions, no model math."""
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows, off = [], 0
    while True:
        page = (sb.table("closed_positions")
                .select("r_multiple,realized_pnl,charges,entry_date")
                .eq("framework", framework)
                .gte("entry_date", since)
                .range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000

    rs = [_f(r.get("r_multiple")) for r in rows if _f(r.get("r_multiple")) is not None]
    pnls = [_f(r.get("realized_pnl")) or 0.0 for r in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "n": len(rows),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(rs) * 100, 1) if rs else None,
        "avg_r": round(statistics.fmean(rs), 4) if rs else None,
        "total_r": round(sum(rs), 3) if rs else None,
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
    }


def _open_position_summary(sb, framework: str) -> dict:
    rows = (sb.table("open_positions").select("symbol,unrealized_pnl,status")
            .eq("framework", framework).execute().data) or []
    live = [r for r in rows if (r.get("status") or "").upper() == "ACTIVE"]
    upnl = sum(_f(r.get("unrealized_pnl")) or 0.0 for r in live)
    return {"open_count": len(live),
           "symbols": sorted(r.get("symbol") for r in live if r.get("symbol")),
           "unrealized_pnl": round(upnl, 2)}


def _engine_standings(sb, days: int) -> list[dict]:
    """Reuses engine_scorecard's own dedup+gross-reconstruction logic rather
    than a second copy of it — see that module's header for why the naive
    (one-row-per-cycle) count is wrong."""
    from tools.engine_scorecard import _intraday_observations
    import collections
    obs = _intraday_observations(sb)
    since = (today_ist() - timedelta(days=days)).isoformat()
    obs = [o for o in obs if o["date"] >= since]
    by_engine: dict[str, list] = collections.defaultdict(list)
    for o in obs:
        by_engine[o["engine"]].append(o)
    out = []
    for eng, xs in sorted(by_engine.items(), key=lambda kv: -len(kv[1])):
        net = [x["gross_r"] - x["cost_r"] for x in xs]
        out.append({"engine": eng, "n": len(xs),
                    "net_r_avg": round(statistics.fmean(net), 4)})
    return out


def snapshot(days: int, label: str, sb=None) -> dict:
    sb = sb or get_supabase()
    data = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "label": label,
        "window_days": days,
        "swing": {
            "open": _open_position_summary(sb, "SWING"),
            "closed": _closed_trade_stats(sb, "SWING", days),
        },
        "intraday": {
            "open": _open_position_summary(sb, "INTRADAY"),
            "closed": _closed_trade_stats(sb, "INTRADAY", days),
            "engines": _engine_standings(sb, days),
        },
    }
    return data


def _print_snapshot(data: dict) -> None:
    logger.info("")
    logger.info(f"  BENCHMARK SNAPSHOT — {data['captured_at']}  "
               f"(commit {data['git_commit']}, {data['window_days']}d window)"
               + (f"  [{data['label']}]" if data.get("label") else ""))
    for book in ("swing", "intraday"):
        b = data[book]
        logger.info("  " + "-" * 74)
        logger.info(f"  {book.upper()}")
        logger.info(f"    open: {b['open']['open_count']} position(s), "
                   f"unrealised {b['open']['unrealized_pnl']:+.2f}  "
                   f"[{', '.join(b['open']['symbols']) or 'none'}]")
        c = b["closed"]
        if c["n"]:
            logger.info(f"    closed ({c['n']}): {c['wins']}W/{c['losses']}L "
                       f"({c['win_rate_pct']}% win) · avg {c['avg_r']:+.3f}R · "
                       f"total {c['total_r']:+.2f}R · net {c['net_pnl']:+.2f} · "
                       f"PF {c['profit_factor']}")
        else:
            logger.info("    closed: none in this window")
        if book == "intraday" and b.get("engines"):
            for e in b["engines"][:8]:
                logger.info(f"      {e['engine']:<8} n={e['n']:<4} "
                           f"net R avg {e['net_r_avg']:+.4f}")
    logger.info("  " + "-" * 74)


def _delta(a, b):
    if a is None or b is None:
        return None
    return round(b - a, 4)


def compare(path_a: str, path_b: str) -> int:
    a = json.loads(Path(path_a).read_text())
    b = json.loads(Path(path_b).read_text())

    logger.info("")
    logger.info(f"  COMPARE")
    logger.info(f"    A: {a['captured_at']} (commit {a['git_commit']}) "
               f"[{a.get('label', '')}]")
    logger.info(f"    B: {b['captured_at']} (commit {b['git_commit']}) "
               f"[{b.get('label', '')}]")
    logger.info("  " + "-" * 74)

    for book in ("swing", "intraday"):
        ca, cb = a[book]["closed"], b[book]["closed"]
        oa, ob = a[book]["open"], b[book]["open"]
        logger.info(f"  {book.upper()}")
        logger.info(f"    open positions:  {oa['open_count']} -> {ob['open_count']}")
        if ca["n"] or cb["n"]:
            d_wr = _delta(ca["win_rate_pct"], cb["win_rate_pct"])
            d_r = _delta(ca["avg_r"], cb["avg_r"])
            d_pnl = _delta(ca["net_pnl"], cb["net_pnl"])
            logger.info(f"    closed trades:   {ca['n']} -> {cb['n']}")
            logger.info(f"    win rate:        {ca['win_rate_pct']} -> "
                       f"{cb['win_rate_pct']}%  ({d_wr:+.1f} pts)" if d_wr is not None
                       else f"    win rate:        {ca['win_rate_pct']} -> {cb['win_rate_pct']}%")
            logger.info(f"    avg R:           {ca['avg_r']} -> {cb['avg_r']}"
                       + (f"  ({d_r:+.4f}R)" if d_r is not None else ""))
            logger.info(f"    net P&L:         {ca['net_pnl']} -> {cb['net_pnl']}"
                       + (f"  ({d_pnl:+.2f})" if d_pnl is not None else ""))
        else:
            logger.info("    closed trades:   none in either snapshot's window")
        logger.info("")

    logger.warning(
        "  This is a STATE comparison, not a causal one — other real trades may "
        "have opened/closed between A and B for reasons unrelated to any code "
        "change (new sessions, market moves). Pair this with "
        "tools.allocator_replay / tools.exit_ladder_replay, which isolate the "
        "POLICY's effect on the same historical data, for a claim about "
        "causation rather than just before/after state.")
    return 0


def _two_most_recent(bench_dir: Path) -> tuple[str, str] | None:
    """The last two snapshots written to `bench_dir`, oldest first (so the
    result reads as A -> B, before -> after) — or None if fewer than two
    exist. Pure enough to unit test without going through argparse."""
    files = sorted(bench_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if len(files) < 2:
        return None
    return str(files[-2]), str(files[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="capture current state")
    sp.add_argument("--days", type=int, default=10)
    sp.add_argument("--label", default="")
    sp.add_argument("--out", default=None)

    cp = sub.add_parser("compare", help="diff two snapshots")
    cp.add_argument("file_a", nargs="?", default=None,
                    help="omit both to auto-compare the two most recent "
                         "snapshots in benchmarks/")
    cp.add_argument("file_b", nargs="?", default=None)

    a = ap.parse_args()
    if a.cmd == "snapshot":
        data = snapshot(a.days, a.label)
        _print_snapshot(data)
        BENCH_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        label_part = "_" + a.label.replace(" ", "_") if a.label else ""
        out = Path(a.out) if a.out else BENCH_DIR / f"{ts}{label_part}.json"
        out.write_text(json.dumps(data, indent=2))
        logger.success(f"  saved: {out}")
        return 0

    fa, fb = a.file_a, a.file_b
    if fa is None or fb is None:
        # THE APPLE-TO-APPLE WORKFLOW IN ONE COMMAND: snapshot before,
        # snapshot after, then `compare` with no arguments. Requiring the
        # operator to go find and paste two timestamped filenames back is
        # exactly the kind of friction that turns a step nobody skips into
        # a step somebody eventually does.
        pair = _two_most_recent(BENCH_DIR)
        if pair is None:
            logger.error(
                f"  fewer than 2 snapshots in {BENCH_DIR} — nothing to "
                f"auto-compare. Run `snapshot` twice (before and after the "
                f"change), or pass two file paths explicitly.")
            return 1
        fa, fb = pair
        logger.info(f"  no files given — comparing the two most recent snapshots:\n"
                   f"    A: {fa}\n    B: {fb}\n"
                   f"  (pass two paths explicitly if that is not the pair you meant)")
    return compare(fa, fb)


if __name__ == "__main__":
    sys.exit(main())
