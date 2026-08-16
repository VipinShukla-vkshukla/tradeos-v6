"""
Bars: fetched once ever, cached on disk, and never silently missing.

A SILENT GAP IS A LIE IN THE DENOMINATOR
-----------------------------------------
Every fetch attempt writes a manifest line whether it succeeded or not. A
symbol-day that failed and left no trace becomes a symbol-day that "had no
setups", which is indistinguishable in every downstream statistic from a
symbol-day the engines correctly passed on. The manifest is what makes coverage
reportable, and every report the harness prints carries a coverage line.

RATE LIMIT
-----------
The repo's own statement is "Kite allows about three a second"
(`outcomes.py:57`). This throttles to 2 req/s through a simple token bucket, and
stops hard after 5 consecutive failures rather than degrading into a partial
fetch that looks complete.

CACHE FORMAT — A DEVIATION FROM THE DESIGN, STATED
---------------------------------------------------
REPLAY_DESIGN §3.3 specifies parquet, one file per symbol-month. This uses
gzipped JSON, one file per symbol-day. The reason is dependency risk, not
preference: parquet needs pyarrow, which is not currently a declared dependency
of this project, and a backtest harness is a bad place to introduce one. At the
resized budget (§3.2: ~4,182 symbol-days, ~1.6M bars) the storage difference is
tens of megabytes and does not matter. The cache key is
`(symbol, date, interval)` either way, so a re-run at a different interval
builds a separate tree rather than mixing two bar sizes.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from dataclasses import dataclass
from datetime import date as _date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import IST, cfg
from intraday.strategies.base import Bar

CACHE_DIR = Path(__file__).resolve().parent / "cache"
MANIFEST = CACHE_DIR / "manifest.jsonl"

_MIN_INTERVAL_S = 0.5          # 2 req/s
_MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class Coverage:
    """What the fetch actually got. Printed on every report."""
    requested: int = 0
    ok: int = 0
    miss: int = 0
    reasons: dict[str, int] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = {}

    @property
    def pct(self) -> float:
        return (self.ok / self.requested * 100.0) if self.requested else 0.0

    def line(self, floor_pct: float = 98.0) -> str:
        head = "INCOMPLETE " if self.pct < floor_pct else ""
        detail = ", ".join(f"{k}={v}" for k, v in sorted(self.reasons.items()))
        return (f"{head}coverage: {self.ok}/{self.requested} symbol-days "
                f"({self.pct:.1f}%), {self.miss} missing"
                + (f" [{detail}]" if detail else ""))


def _cache_path(symbol: str, day: str, interval: str) -> Path:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / "bars" / interval / safe / f"{day}.json.gz"


def _write_manifest(rec: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def load_cached(symbol: str, day: str, interval: str) -> list[Bar] | None:
    """Bars from disk, or None if this symbol-day was never fetched."""
    path = _cache_path(symbol, day, interval)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as e:
        logger.warning(f"  cache unreadable {path.name}: {e}")
        return None
    return [_to_bar(r) for r in raw]


def _to_bar(r: dict) -> Bar:
    ts = r["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    return Bar(ts=ts, open=float(r["open"]), high=float(r["high"]),
               low=float(r["low"]), close=float(r["close"]),
               volume=float(r.get("volume") or 0))


def _store(symbol: str, day: str, interval: str, bars: list[Bar]) -> None:
    path = _cache_path(symbol, day, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"ts": b.ts.isoformat(), "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume}
               for b in bars]
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)


class BarSource:
    """
    Cached minute bars for a set of symbol-days. Fetch once, ever.

    `kite` may be None when every requested symbol-day is already cached — which
    is the normal case for a re-run, and lets the verification path work with no
    broker session at all.
    """

    def __init__(self, kite=None, interval: str | None = None):
        self.kite = kite
        self.interval = interval or cfg("intraday_bar_interval", "minute")
        self.coverage = Coverage()
        self._tokens: dict[str, int] = {}
        self._last_call = 0.0
        self._consecutive_failures = 0

    # ── token lookup ────────────────────────────────────────────────────────
    def resolve_tokens(self, symbols: list[str]) -> dict[str, int]:
        """
        Instrument tokens, the same way the live path gets them (`kite.ltp`).

        Batched — one call for the whole list, not one per symbol.
        """
        missing = [s for s in symbols if s not in self._tokens]
        if not missing or self.kite is None:
            return self._tokens
        try:
            got = self.kite.ltp([f"NSE:{s}" for s in missing]) or {}
            for key, meta in got.items():
                self._tokens[key.split(":", 1)[1]] = meta["instrument_token"]
        except Exception as e:
            logger.warning(f"  token lookup failed for {len(missing)} symbols: {e}")
        return self._tokens

    # ── throttle ────────────────────────────────────────────────────────────
    def _throttle(self) -> None:
        wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # ── the one fetch ───────────────────────────────────────────────────────
    def get(self, symbol: str, day: str) -> list[Bar]:
        """
        Every bar for one symbol on one day. Cache first, broker only on a miss.

        Returns [] on any failure, and records WHY in the manifest. An empty
        return is never silently equivalent to "no setups" — the coverage line
        carries it.
        """
        self.coverage.requested += 1

        cached = load_cached(symbol, day, self.interval)
        if cached is not None:
            self.coverage.ok += 1
            return cached

        if self.kite is None:
            self._miss(symbol, day, "no_broker_session")
            return []
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._miss(symbol, day, "halted_after_consecutive_failures")
            return []

        token = self.resolve_tokens([symbol]).get(symbol)
        if not token:
            self._miss(symbol, day, "no_instrument_token")
            return []

        d = _date.fromisoformat(day)
        self._throttle()
        try:
            raw = self.kite.historical_data(token, d, d, self.interval) or []
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            self._miss(symbol, day, f"fetch_error:{type(e).__name__}")
            return []

        bars: list[Bar] = []
        for b in raw:
            ts = b["date"]
            if ts.tzinfo is None:
                ts = IST.localize(ts)
            bars.append(Bar(ts=ts, open=float(b["open"]), high=float(b["high"]),
                            low=float(b["low"]), close=float(b["close"]),
                            volume=float(b.get("volume") or 0)))

        if not bars:
            self._miss(symbol, day, "empty_response")
            return []

        _store(symbol, day, self.interval, bars)
        self.coverage.ok += 1
        _write_manifest({"symbol": symbol, "date": day, "interval": self.interval,
                         "rows": len(bars), "fetched_at": datetime.now(IST),
                         "status": "OK"})
        return bars

    def _miss(self, symbol: str, day: str, reason: str) -> None:
        self.coverage.miss += 1
        self.coverage.reasons[reason] = self.coverage.reasons.get(reason, 0) + 1
        _write_manifest({"symbol": symbol, "date": day, "interval": self.interval,
                         "rows": 0, "fetched_at": datetime.now(IST),
                         "status": "MISS", "reason": reason})
