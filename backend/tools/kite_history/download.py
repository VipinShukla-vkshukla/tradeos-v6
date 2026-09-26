"""
Download Kite's historical candles into the local folder, resumably and politely.

    python -m tools.kite_history probe --symbol RELIANCE          how far back does Kite serve each interval?
    python -m tools.kite_history backfill --interval minute       everything, newest first, most liquid first
    python -m tools.kite_history backfill --interval day
    python -m tools.kite_history update --interval minute         bring finished symbols up to date
    python -m tools.kite_history status | verify

HOW A SYMBOL IS FETCHED. Windows walk BACKWARD from the last completed session, each within Kite's per-request
limit (minute: 60 days), until Kite stops returning candles. A run of empty windows after data means the
history has ended; a long run of empty windows before any data means the instrument has none. A symbol is
written and marked `done` only when its walk completes, so a crash re-does at most one symbol.

WHAT IT WILL NOT DO
  * run while the market is open (Mon-Fri 08:50-15:45 IST) unless told to: the live daemon shares Kite's
    3 requests/second historical limit, and this run must never make it late.
  * exceed one request per --min-interval seconds across all workers (default 0.4 s = 2.5 req/s).
  * carry on after the access token expires: it stops, says so, and keeps everything already written.
    Refresh the token (python -m kite.token_manager) and rerun the same command; finished symbols are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from tools.kite_history import state, store
from tools.kite_history.universe import DEFAULT_SEGMENTS, Target

IST = ZoneInfo("Asia/Kolkata")

# Calendar days per request, a little under Kite's documented maximum for each interval.
MAX_SPAN_DAYS = {"minute": 58, "3minute": 98, "5minute": 98, "10minute": 98,
                 "15minute": 198, "30minute": 198, "60minute": 398, "day": 1990}
FLOOR = {"minute": date(2010, 1, 1), "day": date(1990, 1, 1)}
DEFAULT_FLOOR = date(2010, 1, 1)
END_GAP_DAYS = 110          # this many consecutive empty days after data: the history has ended
NO_DATA_DAYS = 400          # this many empty days before any data: the instrument has none
MAX_ATTEMPTS = 5
QUIET_START, QUIET_END = (8, 50), (15, 45)


class TokenExpired(RuntimeError):
    """The Kite access token is no longer valid; nothing more can be fetched until it is refreshed."""


class Stopped(RuntimeError):
    """Another worker asked everything to stop (token expired, or Ctrl-C)."""


class InstrumentRefused(RuntimeError):
    """Kite rejected this instrument or request as invalid (not a transient failure)."""


class FetchFailed(RuntimeError):
    """A window still failed after every retry."""


# ── the real client, without the slow part ──────────────────────────────────

class FastKite:
    """Kite's historical endpoint with pykiteconnect's per-candle dateutil parse removed. That parse costs about
    80 microseconds a candle, i.e. ~1.2 s of CPU (under the GIL) for one 58-day request, which capped the whole
    download at ~1 request/second however many threads ran. The raw rows are parsed in one vectorised call
    instead (store.frame_from_rows), leaving Kite's own 3 requests/second as the limit."""

    def __init__(self, kite):
        self.kite = kite

    def fetch_frame(self, token: int, start: date, end: date, interval: str, with_oi: bool) -> pd.DataFrame:
        data = self.kite._get("market.historical",
                              url_args={"instrument_token": token, "interval": interval},
                              params={"from": start.isoformat(), "to": end.isoformat(), "interval": interval,
                                      "continuous": 0, "oi": 1 if with_oi else 0})
        return store.frame_from_rows(data.get("candles") or [], with_oi)

    def __getattr__(self, name):
        return getattr(self.kite, name)


def _fetch_frame(kite, token: int, start: date, end: date, interval: str, with_oi: bool) -> pd.DataFrame:
    if hasattr(kite, "fetch_frame"):
        return kite.fetch_frame(token, start, end, interval, with_oi)
    return store.frame_from_candles(kite.historical_data(token, start, end, interval, False, with_oi) or [], with_oi)


# ── what the run is actually doing ──────────────────────────────────────────

class Stats:
    """Counters behind the heartbeat line: requests, retries, errors by type, time spent waiting on Kite."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = self.retries = 0
        self.latency = 0.0
        self.errors: dict[str, int] = {}

    def ok(self, seconds: float) -> None:
        with self._lock:
            self.requests += 1
            self.latency += seconds

    def fail(self, e: BaseException, seconds: float) -> None:
        with self._lock:
            self.retries += 1
            self.latency += seconds
            name = type(e).__name__
            self.errors[name] = self.errors.get(name, 0) + 1

    def snapshot(self) -> tuple[int, int, float, dict]:
        with self._lock:
            return self.requests, self.retries, self.latency, dict(self.errors)


STATS = Stats()


# ── pacing ──────────────────────────────────────────────────────────────────

class Limiter:
    """One request clock shared by every worker thread."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval, self._clock, self._sleep = min_interval, clock, sleep
        self._last, self._lock = -1e9, threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self.min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
            self._last = self._clock()


def in_quiet_window(now: datetime) -> bool:
    """True during the hours the live system needs the API to itself (weekdays 08:50-15:45 IST)."""
    now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return QUIET_START <= hm < QUIET_END


def last_completed_session(now: datetime) -> date:
    """The most recent weekday whose candles are final (today only once the close has settled)."""
    now = now.astimezone(IST)
    d = now.date() if (now.hour, now.minute) >= QUIET_END else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ── windows ─────────────────────────────────────────────────────────────────

def backward_windows(end: date, span_days: int, floor: date) -> Iterator[tuple[date, date]]:
    """Inclusive (start, end) windows of at most `span_days` calendar days, newest first, never before `floor`."""
    cur_end = end
    while cur_end >= floor:
        start = max(floor, cur_end - timedelta(days=span_days - 1))
        yield start, cur_end
        cur_end = start - timedelta(days=1)


def forward_windows(start: date, end: date, span_days: int) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        e = min(end, cur + timedelta(days=span_days - 1))
        yield cur, e
        cur = e + timedelta(days=1)


def _windows_needed(days: int, span: int) -> int:
    return max(1, -(-days // span))


def classify_error(e: BaseException) -> str:
    """'token' | 'retry' | 'input'. Kite's own exception types decide first, then the message."""
    name, msg = type(e).__name__, str(e).lower()
    if name == "TokenException" or "access_token" in msg or "incorrect `api_key`" in msg:
        return "token"
    if name == "InputException":
        return "input"
    if any(k in msg for k in ("too many requests", "429", "timed out", "timeout", "connection", "temporarily", "502", "503", "504")):
        return "retry"
    if name in ("NetworkException", "DataException", "GeneralException") or isinstance(e, (OSError, TimeoutError)):
        return "retry"
    return "retry"


def fetch_window(kite, limiter: Limiter, token: int, start: date, end: date, interval: str, with_oi: bool,
                 stop: threading.Event, *, gate: Callable[[], None] = lambda: None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = MAX_ATTEMPTS) -> pd.DataFrame:
    last: BaseException | None = None
    for attempt in range(attempts):
        if stop.is_set():
            raise Stopped("stopped")
        gate()
        limiter.wait()
        t0 = time.monotonic()
        try:
            out = _fetch_frame(kite, token, start, end, interval, with_oi)
            STATS.ok(time.monotonic() - t0)
            return out
        except BaseException as e:                                  # noqa: BLE001 - classified below
            STATS.fail(e, time.monotonic() - t0)
            kind = classify_error(e)
            if kind == "token":
                raise TokenExpired(str(e)) from e
            if kind == "input":
                raise InstrumentRefused(str(e)) from e
            last = e
            sleep(min(30.0, 2.0 * (2 ** attempt)))
    raise FetchFailed(f"{start}..{end}: {type(last).__name__}: {str(last)[:100]}")


# ── one symbol ──────────────────────────────────────────────────────────────

class Outcome(NamedTuple):
    status: str
    rows: int = 0          # candles fetched by this call
    requests: int = 0


@dataclass
class Ctx:
    kite: object
    root: Path
    progress: state.Progress
    limiter: Limiter
    stop: threading.Event
    end: date
    interval: str
    floor: date
    gate: Callable[[], None] = lambda: None
    sleep: Callable[[float], None] = time.sleep


def backfill_symbol(ctx: Ctx, t: Target) -> Outcome:
    """Walk one instrument's history backward, store it, record the outcome."""
    span = MAX_SPAN_DAYS.get(ctx.interval, 58)
    end_windows = _windows_needed(END_GAP_DAYS, span)
    lead_windows = _windows_needed(NO_DATA_DAYS, span)
    frames: list[pd.DataFrame] = []          # one compact frame per window: a full history as dicts would need ~1 GB
    requests = empty_streak = 0
    try:
        for start, end in backward_windows(ctx.end, span, ctx.floor):
            got = fetch_window(ctx.kite, ctx.limiter, t.token, start, end, ctx.interval, t.with_oi, ctx.stop,
                               gate=ctx.gate, sleep=ctx.sleep)
            requests += 1
            if not got.empty:
                frames.append(got)
                empty_streak = 0
                continue
            empty_streak += 1
            if frames and empty_streak >= end_windows:
                break
            if not frames and empty_streak >= lead_windows:
                break
    except (TokenExpired, Stopped):
        raise
    except InstrumentRefused as e:
        ctx.progress.record(ctx.interval, t.segment, t.symbol, state.SKIPPED, token=t.token, requests=requests, note=str(e)[:200])
        return Outcome(state.SKIPPED, 0, requests)
    except FetchFailed as e:
        ctx.progress.record(ctx.interval, t.segment, t.symbol, state.ERROR, token=t.token, requests=requests, note=str(e)[:200])
        return Outcome(state.ERROR, 0, requests)
    if not frames:
        ctx.progress.record(ctx.interval, t.segment, t.symbol, state.EMPTY, token=t.token, requests=requests)
        return Outcome(state.EMPTY, 0, requests)
    df = store.combine(frames)
    store.write_frame(ctx.root, ctx.interval, t.segment, t.symbol, df, t.with_oi)
    ctx.progress.record(ctx.interval, t.segment, t.symbol, state.DONE, token=t.token, first_ts=df["ts"].min(),
                        last_ts=df["ts"].max(), rows=len(df), requests=requests)
    return Outcome(state.DONE, len(df), requests)


def update_symbol(ctx: Ctx, t: Target, overlap_days: int = 3) -> Outcome:
    """Fetch what is new since the last stored candle (a few days of overlap, deduplicated on write)."""
    prev = ctx.progress.get(ctx.interval, t.segment, t.symbol)
    if not prev or prev["status"] != state.DONE or not prev["last_ts"]:
        return backfill_symbol(ctx, t)
    last = pd.Timestamp(prev["last_ts"]).date()
    start = last - timedelta(days=overlap_days)
    if start > ctx.end:
        return Outcome(state.DONE)
    span = MAX_SPAN_DAYS.get(ctx.interval, 58)
    frames, requests = [], 0
    try:
        for s, e in forward_windows(start, ctx.end, span):
            got = fetch_window(ctx.kite, ctx.limiter, t.token, s, e, ctx.interval, t.with_oi, ctx.stop,
                               gate=ctx.gate, sleep=ctx.sleep)
            requests += 1
            if not got.empty:
                frames.append(got)
    except (TokenExpired, Stopped):
        raise
    except InstrumentRefused as e:
        ctx.progress.record(ctx.interval, t.segment, t.symbol, state.SKIPPED, token=t.token, requests=requests, note=str(e)[:200])
        return Outcome(state.SKIPPED, 0, requests)
    except FetchFailed as e:
        ctx.progress.record(ctx.interval, t.segment, t.symbol, state.ERROR, token=t.token, requests=requests, note=str(e)[:200])
        return Outcome(state.ERROR, 0, requests)
    new_rows = 0
    if frames:
        df = store.combine(frames)
        new_rows = len(df)
        store.write_frame(ctx.root, ctx.interval, t.segment, t.symbol, df, t.with_oi)
    span_now = store.stored_span(ctx.root, ctx.interval, t.segment, t.symbol)
    first, lastts, rows = span_now if span_now else (prev["first_ts"], prev["last_ts"], prev["rows"])
    ctx.progress.record(ctx.interval, t.segment, t.symbol, state.DONE, token=t.token, first_ts=first, last_ts=lastts,
                        rows=rows, requests=requests)
    return Outcome(state.DONE, new_rows, requests)


# ── the run ─────────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    counts: dict = field(default_factory=dict)
    rows: int = 0
    requests: int = 0
    seconds: float = 0.0
    token_expired: bool = False
    interrupted: bool = False
    skipped_already_done: int = 0

    def line(self) -> str:
        c = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items())) or "nothing to do"
        flag = "  TOKEN EXPIRED - refresh it and rerun" if self.token_expired else ("  INTERRUPTED" if self.interrupted else "")
        return f"{c}; {self.skipped_already_done} already done; {self.requests} requests in {self.seconds / 60:.1f} min{flag}"


def run(kite, root: Path, targets: Sequence[Target], interval: str, *, mode: str = "backfill", workers: int = 3,
        min_interval: float = 0.4, since: date | None = None, allow_market_hours: bool = False, redo: bool = False,
        now_fn: Callable[[], datetime] = lambda: datetime.now(IST), sleep: Callable[[float], None] = time.sleep,
        clock=time.monotonic, progress: state.Progress | None = None, log: Callable[[str], None] = print,
        report_every: int = 25, heartbeat_s: float = 60.0) -> RunResult:
    root = Path(root)
    prog = progress or state.Progress(root / "_state" / "progress.db")
    stop = threading.Event()
    limiter = Limiter(min_interval, clock=clock, sleep=sleep)
    quiet_logged = threading.Event()

    def gate() -> None:
        while not allow_market_hours and in_quiet_window(now_fn()) and not stop.is_set():
            if not quiet_logged.is_set():
                log("  market hours (Mon-Fri 08:50-15:45 IST): paused so the live system keeps the API to itself")
                quiet_logged.set()
            sleep(30)

    ctx = Ctx(kite=kite, root=root, progress=prog, limiter=limiter, stop=stop, end=last_completed_session(now_fn()),
              interval=interval, floor=since or FLOOR.get(interval, DEFAULT_FLOOR), gate=gate, sleep=sleep)
    todo = []
    res = RunResult()
    for t in targets:
        st = prog.status_of(interval, t.segment, t.symbol)
        if mode == "backfill" and st in (state.DONE, state.EMPTY, state.SKIPPED) and not redo:
            res.skipped_already_done += 1
        elif mode == "update" and st in (state.EMPTY, state.SKIPPED):
            res.skipped_already_done += 1
        else:
            todo.append(t)
    log(f"{mode} {interval}: {len(todo)} to do, {res.skipped_already_done} already finished, window {ctx.floor}..{ctx.end}")
    lock = threading.Lock()
    finished = [0]
    t0 = clock()

    def work(t: Target) -> None:
        if stop.is_set():
            return
        try:
            out = update_symbol(ctx, t) if mode == "update" else backfill_symbol(ctx, t)
        except TokenExpired:
            res.token_expired = True
            stop.set()
            return
        except Stopped:
            return
        with lock:
            res.counts[out.status] = res.counts.get(out.status, 0) + 1
            res.rows += out.rows
            res.requests += out.requests
            finished[0] += 1
            if finished[0] % report_every == 0 or finished[0] == len(todo):
                el = clock() - t0
                eta = el / finished[0] * (len(todo) - finished[0]) / 60 if finished[0] else 0
                log(f"  {finished[0]}/{len(todo)} symbols  {res.requests} requests  {el / 60:.1f} min  ~{eta:.0f} min left  "
                    + ", ".join(f"{k} {v}" for k, v in sorted(res.counts.items())))

    beat_stop = threading.Event()

    def heartbeat() -> None:
        last = (STATS.snapshot(), time.monotonic())
        while not beat_stop.wait(heartbeat_s):
            snap, now = STATS.snapshot(), time.monotonic()
            (r0, _, l0, _), t_prev = last[0], last[1]
            r1, retries, lat, errs = snap
            dt = max(1e-9, now - t_prev)
            avg = (lat - l0) / max(1, r1 - r0)
            log(f"  heartbeat: {(r1 - r0) / dt:.2f} req/s over the last {dt:.0f}s (avg {avg:.1f}s per request), "
                f"{r1} requests, {retries} retries {errs if errs else ''}")
            last = (snap, now)

    beat = threading.Thread(target=heartbeat, daemon=True) if heartbeat_s and heartbeat_s > 0 else None
    if beat:
        beat.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
    except KeyboardInterrupt:
        stop.set()
        res.interrupted = True
    finally:
        beat_stop.set()
    res.seconds = clock() - t0
    if progress is None:
        prog.close()
    return res
