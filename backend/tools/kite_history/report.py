"""
Coverage and integrity report for the Kite history folder: what is there, how far back it goes, how big it is,
and whether it passes the integrity checks. Read-only.

    python -m tools.kite_history report                 # coverage from the progress database, fast
    python -m tools.kite_history report --integrity     # plus a full verify pass over every stored symbol (slow)
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd

from tools.kite_history import state, store, verify


def coverage_frame(root: Path, interval: str) -> pd.DataFrame:
    """One row per symbol the progress database knows about, with its outcome and stored span."""
    prog = state.Progress(Path(root) / "_state" / "progress.db")
    with prog._lock:
        cur = prog._db.execute(
            "SELECT segment, symbol, status, first_ts, last_ts, rows, requests, note FROM symbols WHERE interval=?", (interval,))
        df = pd.DataFrame(cur.fetchall(), columns=["segment", "symbol", "status", "first_ts", "last_ts", "rows", "requests", "note"])
    prog.close()
    for c in ("first_ts", "last_ts"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert(store.TZ)
    return df


def folder_gb(root: Path, interval: str, segment: str) -> float:
    base = Path(root) / interval / store.safe_name(segment)
    return sum(p.stat().st_size for p in base.rglob("*.parquet")) / 1e9 if base.exists() else 0.0


def summarise(cov: pd.DataFrame, root: Path, interval: str) -> dict:
    out = {"interval": interval, "segments": {}, "status": cov["status"].value_counts().to_dict()}
    for seg, g in cov.groupby("segment"):
        done = g[g["status"] == state.DONE]
        years = (done["last_ts"] - done["first_ts"]).dt.days / 365.25
        out["segments"][seg] = {
            "instruments": int(len(g)), "done": int(len(done)), "rows": int(done["rows"].sum()),
            "gb": round(folder_gb(root, interval, seg), 4),
            "history_years_median": round(float(years.median()), 1) if len(done) else None,
            "history_years_max": round(float(years.max()), 1) if len(done) else None,
            "earliest": str(done["first_ts"].min()) if len(done) else None,
            "starts_by_year": {int(y): int(n) for y, n in done["first_ts"].dt.year.value_counts().sort_index().items()},
            "not_done": {k: int(v) for k, v in g[g["status"] != state.DONE]["status"].value_counts().items()},
        }
    return out


def integrity(root: Path, interval: str, segments: Sequence[str]) -> dict:
    res = verify.verify_all(Path(root), interval, segments)
    hard = {k: v for k, v in res.items() if verify.has_hard_errors(v)}
    modes = pd.Series([v["bars_mode"] for v in res.values() if v["rows"]]).value_counts().to_dict()
    return {"checked": len(res), "hard_errors": {f"{s}/{n}": {k: r[k] for k in verify.HARD if r[k]} for (s, n), r in hard.items()},
            "symbols_with_big_jumps": sum(1 for v in res.values() if v["big_day_jumps"]),
            "usual_candles_per_day": {int(k): int(v) for k, v in modes.items()},
            "total_rows": int(sum(v["rows"] for v in res.values()))}


def print_report(rep: dict) -> None:
    print(f"interval {rep['interval']}: {rep['status']}")
    for seg, s in rep["segments"].items():
        print(f"\n  {seg}: {s['done']}/{s['instruments']} done, {s['rows']:,} candles, {s['gb']} GB on disk")
        print(f"    history: median {s['history_years_median']} years, longest {s['history_years_max']}, earliest {s['earliest']}")
        print(f"    instruments whose history starts in each year: {s['starts_by_year']}")
        if s["not_done"]:
            print(f"    not stored: {s['not_done']}")
    if "integrity" in rep:
        i = rep["integrity"]
        print(f"\n  integrity: {i['checked']} symbols checked, {len(i['hard_errors'])} with hard errors, "
              f"{i['symbols_with_big_jumps']} with a >25% one-day close jump; usual candles per day {i['usual_candles_per_day']}")
        for k, v in list(i["hard_errors"].items())[:15]:
            print(f"    {k}: {v}")


def build(root: Path, interval: str, with_integrity: bool = False) -> dict:
    cov = coverage_frame(root, interval)
    rep = summarise(cov, root, interval)
    if with_integrity:
        rep["integrity"] = integrity(root, interval, list(rep["segments"]))
    return rep
