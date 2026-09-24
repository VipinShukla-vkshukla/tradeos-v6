"""
Kite daily history and the trend-feature panel built from it (FINDINGS 2026-09-24).

Operator rule: buy/sell inputs come from Kite. delivery_pct is the one exception (NSE
post-settlement, not at Kite) and is still read from stock_data_daily.

Completed sessions only: the request ends yesterday and any bar dated today or later is
dropped again in to_daily_bars, so a forming candle never reaches the indicators.

Cost: one historical_data call per symbol per day plus one ltp call per 200 symbols, on a
background thread spaced by daily_history_spacing_s (0.7s: Kite allows ~3/s and
refresh_contexts() skips a symbol's bars for the cycle when ITS call is rate-limited). The
15s loop only does a dict lookup.

No panel, a failed fetch, or a failed integrity check yields None / ok=False, which the gate
reads as "abstain", never as a bad trend. Integrity guards: adjacent-close jump > 1.4x and a
raw-close cross-check against stock_data_daily. Whether Kite's daily candles are
split-adjusted is unverified.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

from loguru import logger

from config import cfg_bool, cfg_float, cfg_int
from intraday.trend_indicators import DailyBar, feature_panel, max_close_jump

_MIN_BARS_OK = 15
_JUMP_LIMIT = 1.4
_REF_TOL_PCT = 0.5
_TOKEN_CHUNK = 200
_BATCH = 50
_FAIL_RETRY_S = 900.0


def to_daily_bars(raw, today: date) -> list[DailyBar]:
    """Kite candle dicts -> DailyBar, completed sessions only, oldest first."""
    by_date: dict[date, DailyBar] = {}
    for r in raw or []:
        d = r.get("date")
        d = d.date() if isinstance(d, datetime) else d
        if d is None or d >= today:
            continue
        try:
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            v = float(r.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0:
            continue
        by_date[d] = DailyBar(d, o, h, l, c, v)
    return [by_date[d] for d in sorted(by_date)]


def build_panel(raw, today: date, ref_closes: dict | None = None, *,
                jump_limit: float = _JUMP_LIMIT,
                tol_pct: float = _REF_TOL_PCT) -> dict:
    """Feature panel plus integrity fields. Pure — no I/O."""
    bars = to_daily_bars(raw, today)
    panel = feature_panel(bars)
    reasons: list[str] = []
    if len(bars) < _MIN_BARS_OK:
        reasons.append(f"short_history:{len(bars)}")

    jump = max_close_jump(bars[-260:])
    panel["max_close_jump"] = round(jump, 3) if jump else None
    if jump is not None and jump > jump_limit:
        reasons.append(f"close_jump:{jump:.2f}")

    xcheck = None
    if ref_closes:
        diffs = [abs(b.close / ref_closes[b.date] - 1.0) * 100.0
                 for b in bars[-10:]
                 if ref_closes.get(b.date) and ref_closes[b.date] > 0]
        if diffs:
            xcheck = max(diffs)
            if xcheck > tol_pct:
                reasons.append(f"ref_mismatch:{xcheck:.2f}%")
    panel["xcheck_max_diff_pct"] = round(xcheck, 3) if xcheck is not None else None
    panel["ok"] = not reasons
    panel["reason"] = ";".join(reasons) or None
    return panel


def fetch_daily(kite, token: int, start: date, end: date, *, spacing_s: float,
                sleep=time.sleep, retries: int = 5) -> list:
    """One symbol's daily candles, throttled, backing off on rate limits."""
    for attempt in range(retries):
        sleep(spacing_s)
        try:
            return kite.historical_data(token, start, end, "day") or []
        except Exception as e:
            if "Too many requests" not in str(e):
                raise
            sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"rate limited fetching daily bars for token {token}")


class DailyHistory:
    """symbol -> feature panel, built once per trading day on a worker thread."""

    def __init__(self, *, sleep=time.sleep, clock=time.time):
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock
        self._panels: dict[str, dict] = {}
        self._failed: dict[str, float] = {}
        self._tokens: dict[str, int] = {}
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._thread: threading.Thread | None = None
        self._day: date | None = None
        self._started_at: float | None = None

    def features(self, symbol: str) -> dict | None:
        with self._lock:
            return self._panels.get(symbol)

    def stats(self) -> dict:
        with self._lock:
            ok = sum(1 for p in self._panels.values() if p.get("ok"))
            return {"day": self._day, "loaded": len(self._panels), "ok": ok,
                    "unreliable": len(self._panels) - ok,
                    "failed": len(self._failed), "queued": len(self._queue),
                    "started_at": self._started_at}

    def ensure(self, kite, symbols, today: date, ref_closes_fn=None) -> int:
        """Queue any symbol not yet loaded today and make sure a worker is
        draining. Non-blocking. Returns how many symbols were newly queued."""
        if kite is None or not cfg_bool("daily_history_enabled", True):
            return 0
        now = self._clock()
        with self._lock:
            if self._day != today:
                self._panels.clear()
                self._failed.clear()
                self._queue.clear()
                self._queued.clear()
                self._day = today
                self._started_at = now
            fresh = [s for s in symbols
                     if s not in self._panels and s not in self._queued
                     and now - self._failed.get(s, -_FAIL_RETRY_S) >= _FAIL_RETRY_S]
            for s in fresh:
                self._queue.append(s)
                self._queued.add(s)
            if fresh and self._thread is None:
                self._thread = threading.Thread(
                    target=self._worker, args=(kite, today, ref_closes_fn),
                    name="daily-history", daemon=True)
                self._thread.start()
            return len(fresh)

    def _resolve_tokens(self, kite, syms: list[str]) -> None:
        need = [s for s in syms if s not in self._tokens]
        for i in range(0, len(need), _TOKEN_CHUNK):
            chunk = need[i:i + _TOKEN_CHUNK]
            try:
                q = kite.ltp([f"NSE:{s}" for s in chunk]) or {}
            except Exception as e:
                logger.warning(f"  daily_history: token lookup failed — {e}")
                continue
            for key, meta in q.items():
                try:
                    self._tokens[key.split(":", 1)[1]] = int(meta["instrument_token"])
                except (KeyError, TypeError, ValueError):
                    continue

    def _worker(self, kite, today: date, ref_closes_fn) -> None:
        spacing = cfg_float("daily_history_spacing_s", 0.7)
        lookback = cfg_int("daily_history_lookback_days", 420)
        start, end = today - timedelta(days=lookback), today - timedelta(days=1)
        done = 0
        t0 = self._clock()
        try:
            while True:
                with self._lock:
                    batch = [self._queue.popleft() for _ in range(min(_BATCH, len(self._queue)))]
                    for s in batch:
                        self._queued.discard(s)
                    if not batch:
                        self._thread = None
                        break
                    if self._day != today:
                        continue
                self._resolve_tokens(kite, batch)
                refs: dict = {}
                if ref_closes_fn is not None:
                    try:
                        refs = ref_closes_fn(batch) or {}
                    except Exception as e:
                        logger.warning(f"  daily_history: raw-close cross-check "
                                       f"unavailable — {e}")
                for sym in batch:
                    tok = self._tokens.get(sym)
                    if tok is None:
                        with self._lock:
                            self._failed[sym] = self._clock()
                        continue
                    try:
                        raw = fetch_daily(kite, tok, start, end,
                                          spacing_s=spacing, sleep=self._sleep)
                        panel = build_panel(raw, today, refs.get(sym))
                    except Exception as e:
                        logger.debug(f"  daily_history: {sym} failed — {e}")
                        with self._lock:
                            self._failed[sym] = self._clock()
                        continue
                    with self._lock:
                        if self._day == today:
                            self._panels[sym] = panel
                            self._failed.pop(sym, None)
                    done += 1
        except Exception as e:
            logger.warning(f"  daily_history: worker stopped — {e}")
            with self._lock:
                self._thread = None
            return
        s = self.stats()
        logger.info(f"  daily_history: {done} symbol(s) fetched in "
                    f"{self._clock() - t0:.0f}s — {s['ok']} ok, "
                    f"{s['unreliable']} unreliable, {s['failed']} failed")
