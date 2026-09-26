"""
Progress bookkeeping for the Kite history download: one row per (interval, segment, symbol).

The data files are the truth about what is stored; this table is the truth about what was ATTEMPTED and
why it stopped, so a run can be resumed without re-requesting finished symbols and so an empty result
("Kite has nothing for this instrument") is remembered instead of being asked again forever.

Statuses:  done     history fetched back to where Kite stops serving it (or to `since`)
           empty    Kite returned no candles at all for the instrument
           skipped  Kite refused the instrument (e.g. no such instrument for this interval); note says why
           error    stopped by repeated failures; retried on the next run

SQLite, one shared connection behind a lock: the download runs a few worker threads and every write is a
single small statement, so contention is negligible.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DONE, EMPTY, SKIPPED, ERROR = "done", "empty", "skipped", "error"

_DDL = """
CREATE TABLE IF NOT EXISTS symbols (
    key         TEXT PRIMARY KEY,
    interval    TEXT NOT NULL,
    segment     TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    token       INTEGER,
    status      TEXT NOT NULL,
    first_ts    TEXT,
    last_ts     TEXT,
    rows        INTEGER DEFAULT 0,
    requests    INTEGER DEFAULT 0,
    note        TEXT,
    updated_at  TEXT NOT NULL
)"""


def make_key(interval: str, segment: str, symbol: str) -> str:
    return f"{interval}|{segment}|{symbol}"


class Progress:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(_DDL)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, interval: str, segment: str, symbol: str, status: str, *, token: int | None = None,
               first_ts=None, last_ts=None, rows: int = 0, requests: int = 0, note: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO symbols (key, interval, segment, symbol, token, status, first_ts, last_ts, rows, requests, note, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET token=excluded.token, status=excluded.status, first_ts=excluded.first_ts,"
                " last_ts=excluded.last_ts, rows=excluded.rows, requests=excluded.requests, note=excluded.note,"
                " updated_at=excluded.updated_at",
                (make_key(interval, segment, symbol), interval, segment, symbol, token, status,
                 None if first_ts is None else str(first_ts), None if last_ts is None else str(last_ts),
                 int(rows), int(requests), note, datetime.now().isoformat(timespec="seconds")))
            self._db.commit()

    def get(self, interval: str, segment: str, symbol: str) -> dict | None:
        with self._lock:
            cur = self._db.execute("SELECT status, token, first_ts, last_ts, rows, requests, note, updated_at FROM symbols WHERE key=?",
                                   (make_key(interval, segment, symbol),))
            r = cur.fetchone()
        if r is None:
            return None
        return dict(zip(("status", "token", "first_ts", "last_ts", "rows", "requests", "note", "updated_at"), r))

    def status_of(self, interval: str, segment: str, symbol: str) -> str | None:
        r = self.get(interval, segment, symbol)
        return r["status"] if r else None

    def summary(self, interval: str | None = None) -> dict:
        q = "SELECT interval, segment, status, COUNT(*), COALESCE(SUM(rows),0), COALESCE(SUM(requests),0) FROM symbols"
        args: tuple = ()
        if interval:
            q += " WHERE interval=?"
            args = (interval,)
        q += " GROUP BY interval, segment, status ORDER BY interval, segment, status"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return {(i, s, st): {"symbols": n, "rows": r, "requests": q_} for i, s, st, n, r, q_ in rows}

    def with_status(self, interval: str, status: str) -> list[tuple[str, str]]:
        with self._lock:
            cur = self._db.execute("SELECT segment, symbol FROM symbols WHERE interval=? AND status=? ORDER BY segment, symbol",
                                   (interval, status))
            return list(cur.fetchall())
