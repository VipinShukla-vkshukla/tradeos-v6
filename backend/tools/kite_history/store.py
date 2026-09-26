"""
The on-disk layout of the Kite history folder, and nothing else: paths, schema, atomic writes, merge, read.

    <root>/
      instruments/all_<YYYY-MM-DD>.parquet     the full instrument master, one dump per day
      minute/<SEGMENT>/<SYMBOL>/<YYYY>.parquet  one-minute candles, one file per symbol per year
      day/<SEGMENT>/<SYMBOL>.parquet            daily candles, one file per symbol
      <interval>/...                            any other Kite interval, laid out like minute or day
      _state/progress.db, download.log          bookkeeping; safe to delete only with the data

Columns: ts (timestamp, seconds, Asia/Kolkata), open, high, low, close (float64), volume (int64), and oi
(int64) for derivatives only. Prices are exactly what Kite returned: NOT adjusted for splits, bonuses or
demergers. The candle timestamp is the START of the candle (09:15 is the first minute of the session).

Kite's own limits shape this: minute candles reach at most ~60 days per request and history is served
per instrument; there is no tick, depth or order-book history, so a candle is the finest thing that exists.

A file is never edited in place: it is written to a temporary name and renamed, so a crash leaves the
previous version intact.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

TZ = "Asia/Kolkata"
DEFAULT_ROOT = Path(os.environ.get("KITE_HISTORY_DIR", r"D:\kite_history"))
PRICE_COLS = ("open", "high", "low", "close")
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Minute-type intervals are one file per year; the daily interval is one file per symbol.
_PER_YEAR = {"minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute"}


def schema(with_oi: bool) -> pa.Schema:
    fields = [("ts", pa.timestamp("s", tz=TZ)), ("open", pa.float64()), ("high", pa.float64()),
              ("low", pa.float64()), ("close", pa.float64()), ("volume", pa.int64())]
    if with_oi:
        fields.append(("oi", pa.int64()))
    return pa.schema(fields)


def safe_name(symbol: str) -> str:
    """A tradingsymbol as a folder name that Windows accepts and that still reads as the symbol."""
    s = _ILLEGAL.sub("_", str(symbol)).strip().rstrip(".")
    if not s:
        raise ValueError("empty symbol")
    return f"_{s}" if s.upper() in _RESERVED else s


def symbol_dir(root: Path, interval: str, segment: str, symbol: str) -> Path:
    return Path(root) / interval / safe_name(segment) / safe_name(symbol)


def path_for(root: Path, interval: str, segment: str, symbol: str, year: int | None = None) -> Path:
    if interval in _PER_YEAR:
        if year is None:
            raise ValueError(f"{interval} is stored one file per year; a year is required")
        return symbol_dir(root, interval, segment, symbol) / f"{year}.parquet"
    return Path(root) / interval / safe_name(segment) / f"{safe_name(symbol)}.parquet"


def frame_from_candles(candles: Sequence[dict], with_oi: bool = False) -> pd.DataFrame:
    """Kite's list of candle dicts (as pykiteconnect returns them) as a typed, sorted, de-duplicated frame."""
    if not candles:
        return _empty(with_oi)
    df = pd.DataFrame(list(candles)).rename(columns={"date": "ts"})
    ts = pd.to_datetime(df["ts"])
    ts = ts.dt.tz_localize(TZ) if ts.dt.tz is None else ts.dt.tz_convert(TZ)
    df["ts"] = ts
    return _typed(df, with_oi)


def frame_from_rows(rows: Sequence[Sequence], with_oi: bool = False) -> pd.DataFrame:
    """Kite's raw candle rows [timestamp string, o, h, l, c, volume(, oi)] as a typed frame. The timestamps are
    parsed in one vectorised call: pykiteconnect parses them one at a time with dateutil (~80 microseconds
    each), which caps a whole download at about one request per second whatever the thread count."""
    if not rows:
        return _empty(with_oi)
    width = len(rows[0])
    cols = ["ts", *PRICE_COLS, "volume"] + (["oi"] if width >= 7 else [])
    df = pd.DataFrame(rows, columns=cols)
    try:
        ts = pd.to_datetime(df["ts"], format="%Y-%m-%dT%H:%M:%S%z", utc=True)
    except (ValueError, TypeError):
        ts = pd.to_datetime(df["ts"], utc=True)
    df["ts"] = ts.dt.tz_convert(TZ)
    return _typed(df, with_oi)


def _typed(df: pd.DataFrame, with_oi: bool) -> pd.DataFrame:
    cols = ["ts", *PRICE_COLS, "volume"] + (["oi"] if with_oi else [])
    df["ts"] = df["ts"].dt.floor("s")
    for c in PRICE_COLS:
        df[c] = df[c].astype("float64")
    df["volume"] = df["volume"].fillna(0).astype("int64")
    if with_oi:
        df["oi"] = (df["oi"] if "oi" in df.columns else pd.Series(0, index=df.index)).fillna(0).astype("int64")
    return _clean(df[cols])


def _empty(with_oi: bool) -> pd.DataFrame:
    df = pd.DataFrame({"ts": pd.to_datetime([]).tz_localize(TZ)})
    for c in PRICE_COLS:
        df[c] = pd.Series(dtype="float64")
    df["volume"] = pd.Series(dtype="int64")
    if with_oi:
        df["oi"] = pd.Series(dtype="int64")
    return df


def combine(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Several candle frames (one per request window) as one sorted, de-duplicated frame."""
    return _clean(pd.concat(list(frames), ignore_index=True))


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop_duplicates("ts", keep="last").sort_values("ts", kind="stable").reset_index(drop=True)


def _write_atomic(df: pd.DataFrame, path: Path, with_oi: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tbl = pa.Table.from_pandas(df, schema=schema(with_oi), preserve_index=False)
    enc = {"ts": "DELTA_BINARY_PACKED", "volume": "DELTA_BINARY_PACKED"}
    if with_oi:
        enc["oi"] = "DELTA_BINARY_PACKED"
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(tbl, tmp, compression="zstd", compression_level=9, use_dictionary=False,
                   use_byte_stream_split=list(PRICE_COLS), column_encoding=enc)
    os.replace(tmp, path)


def read_file(path: Path) -> pd.DataFrame:
    return pq.read_table(path).to_pandas()


def write_frame(root: Path, interval: str, segment: str, symbol: str, df: pd.DataFrame,
                with_oi: bool = False) -> list[Path]:
    """Merge `df` into the symbol's stored history (new rows win on the same timestamp) and rewrite
    only the files it touches. Returns the paths written."""
    if df.empty:
        return []
    df = _clean(df)
    written: list[Path] = []
    if interval in _PER_YEAR:
        for year, part in df.groupby(df["ts"].dt.year):
            p = path_for(root, interval, segment, symbol, int(year))
            merged = _clean(pd.concat([read_file(p), part], ignore_index=True)) if p.exists() else part
            _write_atomic(merged, p, with_oi)
            written.append(p)
    else:
        p = path_for(root, interval, segment, symbol)
        merged = _clean(pd.concat([read_file(p), df], ignore_index=True)) if p.exists() else df
        _write_atomic(merged, p, with_oi)
        written.append(p)
    return written


def files_for(root: Path, interval: str, segment: str, symbol: str) -> list[Path]:
    if interval in _PER_YEAR:
        d = symbol_dir(root, interval, segment, symbol)
        return sorted(d.glob("*.parquet")) if d.exists() else []
    p = path_for(root, interval, segment, symbol)
    return [p] if p.exists() else []


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def _date_only(x) -> bool:
    import datetime as _dt
    if isinstance(x, _dt.datetime):
        return False
    return isinstance(x, _dt.date) or (isinstance(x, str) and len(x.strip()) == 10)


def read(root: Path, interval: str, segment: str, symbol: str, start=None, end=None) -> pd.DataFrame:
    """A symbol's stored candles from `start` to `end`, oldest first. A date (not a datetime) as `end`
    includes that whole day. Empty frame if nothing is stored."""
    parts = [read_file(p) for p in files_for(root, interval, segment, symbol)]
    if not parts:
        return _empty(False)
    df = _clean(pd.concat(parts, ignore_index=True))
    if start is not None:
        df = df[df["ts"] >= _ts(start)]
    if end is not None:
        e = _ts(end)
        df = df[df["ts"] < e + pd.Timedelta(days=1)] if _date_only(end) else df[df["ts"] <= e]
    return df.reset_index(drop=True)


def read_raw(root: Path, interval: str, segment: str, symbol: str) -> pd.DataFrame:
    """The files exactly as stored, concatenated in file order: no de-duplication, no sorting. For integrity
    checks, which must see what is on disk and not what `read` tidies up."""
    parts = [read_file(p) for p in files_for(root, interval, segment, symbol)]
    return pd.concat(parts, ignore_index=True) if parts else _empty(False)


def stored_span(root: Path, interval: str, segment: str, symbol: str):
    """(first ts, last ts, rows) across a symbol's files, from the Parquet footers plus the two edge
    files, without loading the whole history. None if nothing is stored."""
    files = files_for(root, interval, segment, symbol)
    if not files:
        return None
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
    first = read_file(files[0])["ts"].min()
    last = read_file(files[-1])["ts"].max()
    return first, last, rows


def list_symbols(root: Path, interval: str, segment: str) -> list[str]:
    base = Path(root) / interval / safe_name(segment)
    if not base.exists():
        return []
    if interval in _PER_YEAR:
        return sorted(p.name for p in base.iterdir() if p.is_dir())
    return sorted(p.stem for p in base.glob("*.parquet"))
