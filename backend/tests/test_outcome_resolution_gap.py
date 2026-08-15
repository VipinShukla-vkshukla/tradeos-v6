"""
A day is scored COMPLETELY, or it SHOUTS (15-Aug-2026).

WHAT THIS CATCHES
-----------------
`tools.health` reported "1000 detections from 2026-08-14 were never scored".
The number was not a count. It was the PostgREST row cap, and it was the
symptom of one defect appearing in five places at once.

`resolve_day` reads its work queue with

    sb.table("intraday_setups").select("*").eq(trade_date).is_(outcome, null)

and PostgREST silently returns at most 1000 rows. 14-Aug produced 2289
detections. resolve_day ran, was handed 1000, resolved exactly 1000, logged
"1000 resolved" as a SUCCESS, and left 1289 behind with no indication that
anything was outstanding. The day was not unscored — it was HALF scored, which
is worse, because a half-scored day looks exactly like a finished one.

The same cap sat on every other reader of that table:

  · `unresolved_days`   the detector. Its count IS the cap (1000), so the
                        health check under-reports. Worse, the cap can hide
                        an entire DATE: it returns the first 1000 unresolved
                        rows, and if one bad day fills them, every other
                        unscored date is invisible — to the health check AND
                        to `backfill()`, which iterates exactly this list.
  · `review_engines`    the weekly review — the consumer the health message
                        names. It read 1000 of 8324 rows, and the truncation
                        was biased hard toward the OLDEST sessions: 07-28
                        contributed all 236 of its rows while 08-12
                        contributed 3 of 1527 and 08-13 contributed 2 of
                        1167. Engines were being promoted and retired on
                        late-July evidence with the last three weeks almost
                        entirely absent.
  · `engine_scorecard`  the same table, the same silent truncation.
  · `_rehydrate_recorded`  the restart dedup map. Capped, a restart on a
                        >1000-detection day re-records the part of the
                        morning it could not see — which re-inflates the very
                        population the priors and the hurdle are built from.
                        That is the landmine CLAUDE.md already documents,
                        returning through a different door.

WHY THE FAILURE WAS SILENT FOR THREE SESSIONS
---------------------------------------------
Nothing SCHEDULES resolve_day. It is a side-effect of the intraday daemon's
shutdown: `run.py`'s `finally` block, and only when that daemon held the lease.
`backfill()` runs from the same block and `unresolved_days` deliberately
excludes today, so Friday's own daemon could not clean up Friday's remainder —
the earliest repair is the NEXT trading day's daemon exit. 14-Aug was a Friday.
The weekly review runs Sunday. So the gap was guaranteed to be consumed by the
review before anything could close it, every single week, for any session
producing more than 1000 detections. The last three did.

The swing book does not have this problem: `run_pipeline.py` scores it as an
explicit step. Intraday had no equivalent.

WHAT THE TESTS PIN
------------------
Paging, on all five readers. That a day left half-scored is REPORTED as
half-scored rather than logged as a success. That "no broker session" is not
recorded as a completed run. That the date-scored log line names the DATE (it
named the last setup's DIRECTION — `d` was reassigned inside the loop). And
that an unscored past session raises a CRITICAL alert rather than waiting for
someone to run a health check.

THE FIRST TEST GUARDS THE REST FROM BEING VACUOUS. Every paging test below is
only meaningful if the fake actually enforces the cap it claims to, so the
fake is asserted against directly before anything relies on it.
"""

from __future__ import annotations

from unittest.mock import patch

from tests import cfg_ctx

CAP = 1000


# ── a PostgREST that truncates, because the real one does ────────────────────

class _CappedQuery:
    """Honours the filters, and silently returns at most CAP rows.

    `.range(a, b)` is the escape hatch, exactly as it is against the real
    service: an explicit window is served in full (up to CAP per request),
    an unbounded read is truncated with no error and no warning.
    """

    def __init__(self, rows, log):
        self._rows = list(rows)
        self._log = log
        self._neg = False
        self._want: set | None = None
        self._ordered: str | None = None

    def select(self, *a, **k):
        """Record the projection; apply it at execute() time, not now.

        A fake that ignores the select string cannot express the claim that a
        function reads what it asked for — that omission is what let the
        `intraday_priors` dedup defect survive every test in
        test_gated_priors. But applying it EAGERLY is equally unfaithful: the
        real service filters server-side on the full row and projects on the
        way out, so `.select("strategy").gte("trade_date", x)` is a perfectly
        valid query. Projecting first makes trade_date None and silently drops
        every row — a fake failure with no counterpart in production.
        """
        cols = ",".join(str(x) for x in a)
        if cols and "*" not in cols:
            self._want = {c.strip() for c in cols.split(",") if c.strip()}
        return self

    def _project(self, rows):
        if self._want is None:
            return rows
        return [{k: v for k, v in r.items() if k in self._want} for r in rows]

    @property
    def not_(self):
        self._neg = True
        return self

    def _keep(self, fn):
        self._rows = [r for r in self._rows if fn(r)]
        return self

    def eq(self, col, val):
        return self._keep(lambda r: r.get(col) == val)

    def neq(self, col, val):
        return self._keep(lambda r: r.get(col) != val)

    def gte(self, col, val):
        return self._keep(lambda r: str(r.get(col) or "") >= str(val))

    def is_(self, col, _null):
        neg, self._neg = self._neg, False
        if neg:
            return self._keep(lambda r: r.get(col) is not None)
        return self._keep(lambda r: r.get(col) is None)

    def order(self, col, *a, **k):
        self._ordered = col
        self._rows.sort(key=lambda r: r.get(col))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def range(self, start, end):
        """An UNORDERED window is served from a rotated view of the rows.

        PostgreSQL guarantees nothing about row order without an ORDER BY, and
        each page of a paged read is a separate query — so successive windows
        can overlap and can miss rows entirely. Measured on the live book,
        15-Aug-2026: 8324 matching rows paged with no sort returned 8324 rows
        of which 5000 were distinct.

        That is the more dangerous half of this defect, because the COUNT comes
        out right and only the CONTENT is wrong. Rotating here is a cheap
        stand-in for planner instability, and it is what makes
        `test_paging_without_a_sort_key_duplicates_and_drops_rows` able to
        fail. Without it the fake would page perfectly whether or not the
        code asked for a sort, and the test could not tell the two apart.
        """
        self._log.append(("range", start, end))
        rows = self._rows
        if not self._ordered:
            k = (start // max(end - start + 1, 1)) * 37 % max(len(rows), 1)
            rows = rows[k:] + rows[:k]
        self._rows = rows[start:start + min(end - start + 1, CAP)]
        return self

    def execute(self):
        if not self._log or self._log[-1][0] != "range":
            self._log.append(("unranged", len(self._rows)))
            self._rows = self._rows[:CAP]          # ← the silent truncation
        self._rows = self._project(self._rows)
        return self

    @property
    def data(self):
        return self._rows

    @property
    def count(self):
        return len(self._rows)


class _CappedSB:
    def __init__(self, rows):
        self._rows = rows
        self.log: list = []
        self.updates: list = []

    def table(self, name):
        sb = self

        class _T(_CappedQuery):
            def update(self, payload):
                sb.updates.append(payload)
                return _Done()

            def insert(self, payload):
                return _Done()

        return _T(self._rows, self.log)


class _Done:
    data: list = []

    def eq(self, *a, **k):
        return self

    def execute(self):
        return self


def _setups(n, day="2026-08-14", resolved=False, strategy="ORB"):
    """n detections on one day, all LONG, geometry 100 / 99 / 102.

    Symbols are UNIQUE per row. A fixture that reuses 40 names would let the
    rehydration test pass against the capped code — 1000 truncated rows still
    cover all 40 keys — so the fixture itself has to be able to express the
    loss.
    """
    return [{
        "id": 10_000 + i,
        "symbol": f"{day[-2:]}SYM{i}",
        "strategy": strategy,
        "trade_date": day,
        # 03:50 UTC = 09:20 IST, before the fake's first bar, so every row has
        # bars to replay and an UNRESOLVED row means the CODE skipped it
        "ts": f"{day}T03:50:00+00:00",
        "entry": 100.0, "stop": 99.0, "target": 102.0,
        "direction": "LONG",
        "cost_pct": 0.2, "cost_verdict": "TAKEN",
        "risk_pct": 1.0, "confidence": 70,
        "outcome": "TARGET" if resolved else None,
        "outcome_pct": 1.8 if resolved else None,
        "meta": {"sub_engine": strategy},
    } for i in range(n)]


class _FakeKite:
    """One flat bar series that never touches stop or target -> TIMEOUT."""

    def __init__(self, day="2026-08-14"):
        self.day = day
        self.fetches = 0

    def ltp(self, keys):
        return {k: {"instrument_token": 900 + i} for i, k in enumerate(keys)}

    def historical_data(self, token, a, b, interval):
        import datetime
        from config import IST
        base = datetime.datetime.combine(a, datetime.time(9, 30))
        self.fetches += 1
        return [{"date": IST.localize(base + datetime.timedelta(minutes=m)),
                 "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.1}
                for m in range(10)]


# ── 0. the fake is not vacuous ──────────────────────────────────────────────

def test_the_cap_fake_actually_truncates():
    """If this fake did not cap, every paging test below would pass against
    the UNFIXED code and prove nothing. Assert the trap works before trusting
    anything caught in it."""
    sb = _CappedSB(_setups(2289))
    got = sb.table("intraday_setups").select("*").execute().data
    assert len(got) == CAP, (
        f"the fake returned {len(got)} — it must truncate at {CAP} or the "
        f"paging tests are meaningless")


def test_the_cap_fake_serves_an_explicit_range_in_full():
    sb = _CappedSB(_setups(2289))
    got = (sb.table("intraday_setups").select("*").order("id")
             .range(1000, 1999).execute().data)
    assert len(got) == 1000, len(got)
    got = (sb.table("intraday_setups").select("*").order("id")
             .range(2000, 2999).execute().data)
    assert len(got) == 289, len(got)


def test_paging_without_a_sort_key_duplicates_and_drops_rows():
    """The trap that nearly shipped inside the fix for the trap.

    `fetch_all` paged with LIMIT/OFFSET and no ORDER BY. Against the live
    book that returned 8324 rows for 8324 matching rows — a count that
    passes every sanity check — of which only 5000 were distinct. 3324
    duplicates, and 3324 rows never returned at all.

    Worse than truncation: a truncated read is made of real, distinct
    observations, whereas this one silently over-weights whichever rows the
    planner repeated. Every hit rate and prior computed from it is biased and
    nothing about the shape of the result says so.
    """
    from config import fetch_all
    sb = _CappedSB(_setups(2289))
    rows = fetch_all(lambda: sb.table("intraday_setups").select("id"))
    ids = [r["id"] for r in rows]
    assert len(ids) == 2289, f"returned {len(ids)} rows"
    assert len(set(ids)) == 2289, (
        f"{len(ids)} rows but only {len(set(ids))} distinct — paging is not "
        f"ordered, so pages overlap and rows are lost")


def test_fetch_all_lets_a_table_without_an_id_name_its_own_key():
    """`signal_output_daily` has no `id` column. A hardcoded sort key would
    make this function unusable there, or worse, silently wrong."""
    from config import fetch_all
    sb = _CappedSB(_setups(1500))
    rows = fetch_all(lambda: sb.table("intraday_setups").select("ts,symbol"),
                     order_by="symbol")
    assert len(rows) == 1500, len(rows)
    assert len({r["symbol"] for r in rows}) == 1500, "rows lost under a custom key"


# ── 1. resolve_day finishes the day ─────────────────────────────────────────

def test_resolve_day_resolves_every_row_not_the_first_thousand():
    """14-Aug had 2289 unresolved detections. resolve_day resolved exactly
    1000 and reported success."""
    from intraday import outcomes
    sb = _CappedSB(_setups(2289))
    with cfg_ctx({}), patch("kite.kite_client.get_kite", return_value=_FakeKite()):
        res = outcomes.resolve_day("2026-08-14", sb=sb)
    assert res["resolved"] == 2289, (
        f"resolved {res['resolved']} of 2289 — the work queue is still capped")
    assert len(sb.updates) == 2289, len(sb.updates)


def test_resolve_day_reports_what_it_could_not_finish():
    """A day left half-scored must SAY so. The pre-fix log line was
    `1000 resolved ... 1000 timeout` — indistinguishable from a finished day."""
    from intraday import outcomes
    sb = _CappedSB(_setups(2289))
    with cfg_ctx({}), patch("kite.kite_client.get_kite", return_value=_FakeKite()):
        res = outcomes.resolve_day("2026-08-14", sb=sb)
    assert "remaining" in res, f"no `remaining` in {sorted(res)}"
    assert res["remaining"] == 0, res["remaining"]
    assert res.get("complete") is True, res


def test_no_broker_session_is_not_recorded_as_a_finished_day():
    """`{"resolved": 0, "reason": "no_broker"}` is what a Saturday returns.
    It must not be mistakable for "nothing was outstanding"."""
    from intraday import outcomes
    sb = _CappedSB(_setups(300))
    with cfg_ctx({}), patch("kite.kite_client.get_kite", return_value=None):
        res = outcomes.resolve_day("2026-08-14", sb=sb)
    assert res["resolved"] == 0
    assert res.get("complete") is False, res
    assert res.get("remaining") == 300, res


def test_resolve_day_names_the_date_it_scored_not_a_direction():
    """`d` held the trade date, then the row loop reassigned it to the
    setup's DIRECTION, so the one log line that says which session was
    scored read `outcomes LONG: 1000 resolved`."""
    from intraday import outcomes
    sb = _CappedSB(_setups(20))
    with cfg_ctx({}), patch("kite.kite_client.get_kite", return_value=_FakeKite()):
        res = outcomes.resolve_day("2026-08-14", sb=sb)
    assert res.get("date") == "2026-08-14", (
        f"resolve_day reported date={res.get('date')!r} — the date variable is "
        f"still being overwritten by the direction")


# ── 2. the detector sees past the cap, and cannot lose a date ───────────────

def test_unresolved_days_counts_past_the_cap():
    from intraday import outcomes
    sb = _CappedSB(_setups(1289))
    with cfg_ctx({}):
        got = dict(outcomes.unresolved_days(sb))
    assert got.get("2026-08-14") == 1289, (
        f"reported {got} — the health check's number is still the row cap")


def test_unresolved_days_cannot_hide_a_date_behind_the_cap():
    """The failure that makes this urgent: one bad day fills the cap and
    every OTHER unscored date becomes invisible — including to backfill(),
    which iterates this exact list."""
    from intraday import outcomes
    rows = _setups(1200, day="2026-08-14") + _setups(50, day="2026-08-13")
    sb = _CappedSB(rows)
    with cfg_ctx({}):
        got = dict(outcomes.unresolved_days(sb))
    assert "2026-08-13" in got, (
        f"13-Aug vanished behind 14-Aug's 1200 rows — got {got}")
    assert got["2026-08-14"] == 1200 and got["2026-08-13"] == 50, got


def test_unresolved_days_still_excludes_today():
    """Today's setups are legitimately unresolved until the close. A check
    that cries wolf every trading day is a check nobody reads."""
    from intraday import outcomes
    from config import today_ist
    rows = _setups(30, day=today_ist().isoformat())
    sb = _CappedSB(rows)
    with cfg_ctx({}):
        assert outcomes.unresolved_days(sb) == []


# ── 3. the consumers see past the cap ───────────────────────────────────────

def test_weekly_review_reads_every_resolved_row():
    """review_engines read 1000 of 8324 and the truncation favoured the
    OLDEST sessions, so engines were judged on late-July evidence."""
    from tools import weekly_review
    rows = (_setups(1500, day="2026-08-12", resolved=True)
            + _setups(900, day="2026-08-13", resolved=True))
    sb = _CappedSB(rows)
    seen = {}

    real = weekly_review.dedupe_setups

    def spy(rs):
        seen["n"] = len(rs)
        return real(rs)

    with cfg_ctx({}), patch.object(weekly_review, "dedupe_setups", spy):
        weekly_review.review_engines(sb, days=30)
    assert seen.get("n") == 2400, (
        f"review_engines was handed {seen.get('n')} of 2400 rows")


def test_engine_scorecard_reads_every_resolved_row():
    from intraday import outcomes
    sb = _CappedSB(_setups(2400, resolved=True))
    with cfg_ctx({}):
        card = outcomes.engine_scorecard(days=30, sb=sb)
    assert card and card[0]["setups"] == 2400, card


def test_restart_dedup_map_reads_every_row_of_today():
    """Capped, a restart re-records the part of the morning it could not
    see, re-inflating the population the priors are built from."""
    from intraday.engine import IntradayEngine
    from config import today_ist
    rows = _setups(1800, day=today_ist().isoformat())
    sb = _CappedSB(rows)
    eng = object.__new__(IntradayEngine)
    eng.sb = sb
    eng._recorded = {}
    with cfg_ctx({}):
        eng._rehydrate_recorded()
    assert len(eng._recorded) == 1800, (
        f"rehydrated {len(eng._recorded)} of 1800 distinct symbol:engine keys — "
        f"the read is still capped, so a restart would re-record the "
        f"{1800 - len(eng._recorded)} it cannot see")


# ── 4. loudness — an unscored session must ALERT ────────────────────────────

def test_alert_unscored_is_silent_when_every_past_session_is_scored():
    from intraday import outcomes
    sb = _CappedSB(_setups(500, day="2026-08-14", resolved=True))
    with cfg_ctx({}), patch("intraday.notifier.Notifier.send") as send:
        sent = outcomes.alert_unscored(sb=sb)
    assert sent is False
    assert send.call_count == 0, "alerted on a fully scored book"


def test_alert_unscored_fires_when_a_past_session_is_unscored():
    from intraday import outcomes
    sb = _CappedSB(_setups(1289, day="2026-08-14"))
    with cfg_ctx({}), patch("intraday.notifier.Notifier.send",
                            return_value=True) as send:
        sent = outcomes.alert_unscored(sb=sb)
    assert sent is True, "an unscored past session did not alert"
    assert send.call_count == 1, send.call_count
    action = send.call_args[0][0]
    assert action.urgency == "CRITICAL", action.urgency


def test_the_alert_carries_the_true_count_and_the_dates():
    """The operator must be able to act on the message alone — which day,
    how many, and the command that fixes it."""
    from intraday import outcomes
    rows = _setups(1289, day="2026-08-14") + _setups(40, day="2026-08-13")
    sb = _CappedSB(rows)
    with cfg_ctx({}), patch("intraday.notifier.Notifier.send",
                            return_value=True) as send:
        outcomes.alert_unscored(sb=sb)
    a = send.call_args[0][0]
    text = f"{a.headline} {a.detail}"
    assert "2026-08-14" in text, text
    assert "2026-08-13" in text, text
    assert "1329" in text or "1,329" in text, (
        f"the alert must carry the TRUE total (1329), not the row cap — {text}")
    assert "backfill" in text, text


def test_alert_unscored_never_raises():
    """An alert that fails to send must not take down whatever called it —
    the same contract alert_if_stale() carries."""
    from intraday import outcomes
    sb = _CappedSB(_setups(200, day="2026-08-14"))
    with cfg_ctx({}), patch("intraday.notifier.Notifier.send",
                            side_effect=RuntimeError("telegram down")):
        assert outcomes.alert_unscored(sb=sb) is False


def test_alert_unscored_is_reachable_without_a_daemon():
    """The whole point. The failure mode IS the daemon not running, so the
    alert cannot live only inside the daemon."""
    import subprocess
    import sys
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-m", "intraday.outcomes", "--help"],
        cwd=str(backend), capture_output=True, text=True, timeout=90)
    assert "--check-and-alert" in (out.stdout + out.stderr), (
        "no scheduled entry point — the alert can only fire from the daemon, "
        "which is the thing that did not run")


TESTS = [
    ("the cap fake actually truncates", test_the_cap_fake_actually_truncates),
    ("the cap fake serves an explicit range in full",
     test_the_cap_fake_serves_an_explicit_range_in_full),
    ("paging without a sort key duplicates and drops rows",
     test_paging_without_a_sort_key_duplicates_and_drops_rows),
    ("fetch_all lets a table without an id name its own key",
     test_fetch_all_lets_a_table_without_an_id_name_its_own_key),
    ("resolve_day resolves every row, not the first thousand",
     test_resolve_day_resolves_every_row_not_the_first_thousand),
    ("resolve_day reports what it could not finish",
     test_resolve_day_reports_what_it_could_not_finish),
    ("no broker session is not recorded as a finished day",
     test_no_broker_session_is_not_recorded_as_a_finished_day),
    ("resolve_day names the date it scored, not a direction",
     test_resolve_day_names_the_date_it_scored_not_a_direction),
    ("unresolved_days counts past the cap", test_unresolved_days_counts_past_the_cap),
    ("unresolved_days cannot hide a date behind the cap",
     test_unresolved_days_cannot_hide_a_date_behind_the_cap),
    ("unresolved_days still excludes today", test_unresolved_days_still_excludes_today),
    ("weekly review reads every resolved row",
     test_weekly_review_reads_every_resolved_row),
    ("engine scorecard reads every resolved row",
     test_engine_scorecard_reads_every_resolved_row),
    ("restart dedup map reads every row of today",
     test_restart_dedup_map_reads_every_row_of_today),
    ("alert is silent when every past session is scored",
     test_alert_unscored_is_silent_when_every_past_session_is_scored),
    ("an unscored past session alerts", test_alert_unscored_fires_when_a_past_session_is_unscored),
    ("the alert carries the true count and the dates",
     test_the_alert_carries_the_true_count_and_the_dates),
    ("alert never raises", test_alert_unscored_never_raises),
    ("alert is reachable without a daemon",
     test_alert_unscored_is_reachable_without_a_daemon),
]
