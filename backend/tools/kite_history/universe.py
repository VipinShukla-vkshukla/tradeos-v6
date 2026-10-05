"""
What to download: the instrument master and the list of targets derived from it.

DEFAULT SEGMENTS
  NSE       plain mainboard equities and ETFs (no `-XX` suffix, no INAV feed): ~2,600 instruments. The 7,000+
            other rows in Kite's "NSE" dump are bonds, SDLs, T-bills, SME and trade-to-trade series that
            almost never trade; `--segments NSE-ALL` includes them if you want them.
  INDICES   indices on every exchange (Nifty 50, Nifty 500, India VIX, sector and thematic indices, Sensex ...).
  NFO-FUT   the futures contracts listed today, with open interest. Kite serves history only for contracts
            that still exist in its instrument dump, so expired contracts are not obtainable, and options
            (100,000+ contracts) are not in the default set.

Every target is one (segment, tradingsymbol, instrument_token). Order: indices first, then by turnover
(most liquid first) when a turnover map is supplied, so an interrupted run has already stored the
instruments that matter most.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

import pandas as pd

DEFAULT_SEGMENTS = ("INDICES", "NSE", "NFO-FUT")


class Target(NamedTuple):
    segment: str
    symbol: str
    token: int
    name: str
    exchange: str
    instrument_type: str
    with_oi: bool


def _mainboard(symbol: str) -> bool:
    from kite.kite_client import _is_mainboard_symbol
    return _is_mainboard_symbol(symbol)


def build_targets(rows: Iterable[dict], segments: Sequence[str] = DEFAULT_SEGMENTS,
                  turnover: dict[str, float] | None = None, only: Sequence[str] | None = None,
                  limit: int | None = None) -> list[Target]:
    want = {s.upper() for s in segments}
    only_set = {s.upper() for s in only} if only else None
    out: list[Target] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        seg, sym = r.get("segment"), r.get("tradingsymbol")
        if not seg or not sym or not r.get("instrument_token"):
            continue
        key = None
        if seg == "NSE" and (("NSE" in want and _mainboard(sym)) or "NSE-ALL" in want):
            key = ("NSE", False)
        elif seg == "INDICES" and "INDICES" in want:
            key = ("INDICES", False)
        elif seg == "NFO-FUT" and "NFO-FUT" in want:
            key = ("NFO-FUT", True)
        elif seg in want and seg not in ("NSE", "INDICES", "NFO-FUT"):
            key = (seg, seg.startswith(("NFO", "MCX", "CDS", "BFO")))
        if key is None:
            continue
        if only_set is not None and sym.upper() not in only_set:
            continue
        if (key[0], sym) in seen:
            continue
        seen.add((key[0], sym))
        out.append(Target(key[0], sym, int(r["instrument_token"]), r.get("name") or "", r.get("exchange") or "",
                          r.get("instrument_type") or "", key[1]))
    tmap = turnover or {}
    rank = {"INDICES": 0, "NSE": 1, "NFO-FUT": 2}
    out.sort(key=lambda t: (rank.get(t.segment, 3), -float(tmap.get(t.symbol, 0.0)), t.symbol))
    return out[:limit] if limit else out


def save_instruments(root: Path, rows: Sequence[dict], day: str) -> Path:
    """The whole instrument master, as Kite returned it, dated. Useful later for symbol renames,
    lot sizes, tick sizes and the token for any instrument."""
    df = pd.DataFrame(list(rows))
    for c in df.columns:
        if df[c].map(lambda v: hasattr(v, "isoformat")).any():
            df[c] = df[c].map(lambda v: v.isoformat() if hasattr(v, "isoformat") else (v or None))
    path = Path(root) / "instruments" / f"all_{day}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, compression="zstd", index=False)
    return path


def latest_turnover() -> dict[str, float]:
    """value_cr per symbol from the newest `intraday_universe` day, or {} if the database is unreachable
    (the order is a convenience, never a requirement)."""
    try:
        from config import get_supabase
        sb = get_supabase()
        d = sb.table("intraday_universe").select("trade_date").order("trade_date", desc=True).limit(1).execute().data
        if not d:
            return {}
        rows, start = [], 0
        while True:
            page = (sb.table("intraday_universe").select("symbol,value_cr").eq("trade_date", d[0]["trade_date"])
                    .order("symbol").range(start, start + 999).execute().data)
            rows += page
            if len(page) < 1000:
                break
            start += 1000
        return {r["symbol"]: float(r["value_cr"] or 0.0) for r in rows}
    except Exception:
        return {}
