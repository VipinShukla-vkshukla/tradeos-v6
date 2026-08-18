"""
A session that is not over may not be scored (F-27, mechanism A).

WHAT THIS CATCHES
-----------------
`outcomes.resolve_day` prices a TIMEOUT at `bars[-1]["close"]` — the last bar
it happens to be handed — and then never revisits a row that already carries an
outcome (`.is_("outcome", "null")` is its whole work queue). Those two
properties are individually reasonable and jointly a data-corruption engine,
because of WHERE the function is called from:

    intraday/run.py:416   the daemon's `finally` block

That block runs on EVERY exit. A clean 15:40 shutdown, yes — but equally a
crash at 10:12, a Ctrl-C at 11:30, a closed laptop, a `--once` run, a restart
to pick up a config change. On any of those, `historical_data` returns the
session SO FAR, every unresolved setup is scored against a series that ends
mid-morning, anything not yet at its stop or target is written TIMEOUT at the
11:30 price, and the row is frozen: the evening pipeline's `backfill` will not
revisit it, because it is no longer NULL.

The damage is not the pct being slightly off. It is that `intraday_setups` is
the table `scoring.intraday_priors()` and `hurdle`'s arrival distribution are
both built from, so every frozen row prices every future candidate. And the
freeze is silent — a TIMEOUT written at 11:30 is indistinguishable, in the
stored row, from a TIMEOUT written at 15:40.

The measured symptom is 58 same-window contradictions on the live book, 42 of
them a STOP and a TIMEOUT on overlapping windows of the same symbol: one row
scored against the whole session, one scored against a truncated one, and
nothing in the table saying which was which.

WHAT THE TESTS PIN
------------------
1. `resolve_day` REFUSES today's session before the close, and writes nothing.
2. It still SCORES it afterwards — the mirror rule. A guard that cannot pass is
   the same defect wearing a different hat, and the case that matters is the
   real one: the daemon's own cool-down exit must clear the bar, or the fix
   silently moves every day's scoring to the next day's pipeline.
3. A past session is never refused, so `backfill` is untouched.
4. Provenance. Every scored row records WHEN it was scored, by WHICH run, and —
   the one that makes this defect visible at all — THROUGH WHICH BAR.
   `scored_through` on a TIMEOUT is the window end that priced it.
5. The provenance columns are probed ONCE, not per row, and their absence
   degrades to the old two-column write rather than losing the row. PostgREST
   fails the whole statement on one unknown column, and code lands before its
   migration here as a matter of routine.

WHY THE FAKES ARE ASSERTED AGAINST FIRST. Every claim below rests on a bar
series whose END TIME is the variable under test. A fake that returned the same
close whatever end it was given would let all of it pass against the unfixed
code. So the fake is pinned before anything is caught in it.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from tests import cfg_ctx
from tests.test_outcome_resolution_gap import _CappedSB, _setups

DAY = "2026-08-14"


def _ist(day: str, hh: int, mm: int) -> dt.datetime:
    from config import IST
    return IST.localize(dt.datetime.combine(
        dt.date.fromisoformat(day), dt.time(hh, mm)))


class _ClockKite:
    """A flat series from 09:15 to a stated end, at a stated close.

    Neither stop nor target is ever touched, so every setup resolves TIMEOUT —
    which is precisely the outcome `bars[-1]["close"]` prices, and therefore
    the one a truncated series gets wrong. The close is a parameter so that
    "scored at 11:30" and "scored at 15:29" produce DIFFERENT stored numbers;
    without that the freeze is unobservable.
    """

    def __init__(self, day=DAY, end=(15, 29), close=101.0):
        self.day, self.end, self.close = day, end, close
        self.fetches = 0

    def ltp(self, keys):
        return {k: {"instrument_token": 900 + i} for i, k in enumerate(keys)}

    def historical_data(self, token, a, b, interval):
        from config import IST
        self.fetches += 1
        start = dt.datetime.combine(a, dt.time(9, 15))
        last = dt.datetime.combine(a, dt.time(*self.end))
        out, t = [], start
        while t <= last:
            out.append({"date": IST.localize(t), "open": 100.0,
                        "high": 100.5, "low": 99.5,
                        "close": self.close if t == last else 100.1})
            t += dt.timedelta(minutes=1)
        return out


def _migrated(rows: list[dict]) -> list[dict]:
    """The same fixture rows, on a book where migration 082 HAS been applied.

    PostgREST returns every column of a `select("*")` row, NULL ones included,
    so a migrated book's rows carry these three keys with None values and an
    unmigrated book's rows do not carry them at all. That difference is the
    entire probe — which is why the fixture has to be able to express both.
    """
    return [{**r, "scored_at": None, "scored_by": None, "scored_through": None}
            for r in rows]


# ── 0. the fakes are not vacuous ────────────────────────────────────────────

def test_the_clock_fake_ends_where_it_is_told_to():
    k = _ClockKite(end=(11, 30), close=105.0)
    bars = k.historical_data(1, dt.date.fromisoformat(DAY), None, "minute")
    assert bars[-1]["date"].hour == 11 and bars[-1]["date"].minute == 30, bars[-1]["date"]
    assert bars[-1]["close"] == 105.0, bars[-1]["close"]


def test_the_clock_fake_prices_a_different_close_at_a_different_end():
    """If both ends produced the same close, every test below would pass
    against the unfixed code and prove nothing."""
    early = _ClockKite(end=(11, 30), close=105.0).historical_data(
        1, dt.date.fromisoformat(DAY), None, "minute")
    late = _ClockKite(end=(15, 29), close=101.0).historical_data(
        1, dt.date.fromisoformat(DAY), None, "minute")
    assert early[-1]["close"] != late[-1]["close"]
    assert len(late) > len(early), (len(late), len(early))


# ── 1. THE MECHANISM, reproduced before it is stopped ───────────────────────

def test_a_truncated_series_prices_a_timeout_at_the_wrong_close():
    """F-27 mechanism A, in one assertion. Same row, same rule, two answers —
    the only difference is WHEN the scorer was standing there.

    Scored on a PAST date so the guard is not what is being measured here;
    this is the pricing rule itself, and it is not a bug in isolation. It
    becomes one only because run.py calls it mid-session and nothing revisits
    the row afterwards.
    """
    from intraday import outcomes
    early_sb, late_sb = _CappedSB(_setups(1, day=DAY)), _CappedSB(_setups(1, day=DAY))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(11, 30), close=105.0)):
        outcomes.resolve_day(DAY, sb=early_sb, now=_ist("2026-08-15", 9, 0))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        outcomes.resolve_day(DAY, sb=late_sb, now=_ist("2026-08-15", 9, 0))

    early, late = early_sb.updates[0], late_sb.updates[0]
    assert early["outcome"] == late["outcome"] == "TIMEOUT", (early, late)
    assert early["outcome_pct"] != late["outcome_pct"], (
        "the fake cannot express the freeze — both ends priced the same")
    # +4.8 vs +0.8 net of the 0.2 cost: a 4 percentage-point lie on one row.
    assert round(early["outcome_pct"], 1) == 4.8, early
    assert round(late["outcome_pct"], 1) == 0.8, late


def test_the_stored_row_says_which_bar_priced_it():
    """The reason 58 contradictions sat undiagnosed for two days: nothing in
    the row distinguishes a TIMEOUT priced at 11:30 from one priced at 15:29.
    `scored_through` is that column."""
    from intraday import outcomes
    sb = _CappedSB(_migrated(_setups(1, day=DAY)))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(11, 30), close=105.0)):
        outcomes.resolve_day(DAY, sb=sb, now=_ist("2026-08-15", 9, 0))
    got = sb.updates[0].get("scored_through")
    assert got, f"no scored_through in {sorted(sb.updates[0])}"
    assert "11:30" in str(got), (
        f"scored_through={got!r} — it must name the last bar the scorer saw, "
        f"which is the whole diagnostic value of the column")


# ── 2. THE GUARD REFUSES an open session ────────────────────────────────────

def test_resolve_day_refuses_todays_session_before_the_close():
    from intraday import outcomes
    today = "2026-08-14"
    sb = _CappedSB(_setups(50, day=today))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(11, 30), close=105.0)):
        res = outcomes.resolve_day(today, sb=sb, now=_ist(today, 11, 30))
    assert res["resolved"] == 0, (
        f"scored {res['resolved']} row(s) at 11:30 — the session is still open "
        f"and every TIMEOUT written now is frozen at a mid-morning price")
    assert res.get("reason") == "session_open", res


def test_the_refusal_writes_nothing_at_all():
    """A refusal that still wrote some rows would be the worst of both."""
    from intraday import outcomes
    today = "2026-08-14"
    sb = _CappedSB(_setups(50, day=today))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(11, 30), close=105.0)):
        outcomes.resolve_day(today, sb=sb, now=_ist(today, 11, 30))
    assert sb.updates == [], f"{len(sb.updates)} row(s) written during an open session"


def test_a_refusal_is_not_mistakable_for_a_finished_day():
    """`{"resolved": 0}` is also what a fully-scored day returns. The caller in
    run.py branches on `complete`, so a refusal must set it False."""
    from intraday import outcomes
    today = "2026-08-14"
    sb = _CappedSB(_setups(50, day=today))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite()):
        res = outcomes.resolve_day(today, sb=sb, now=_ist(today, 11, 30))
    assert res.get("complete") is False, res
    assert res.get("date") == today, res


def test_the_daemons_default_call_is_the_guarded_one():
    """run.py:416 calls `resolve_day(sb=sb)` — no date. If the default date
    were derived from a different clock than the guard, the guard would be
    checking a day the function is not scoring."""
    from intraday import outcomes
    today = "2026-08-14"
    sb = _CappedSB(_setups(10, day=today))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite()):
        res = outcomes.resolve_day(sb=sb, now=_ist(today, 11, 30))
    assert res.get("date") == today, res
    assert res.get("reason") == "session_open", res


def test_a_future_date_is_refused():
    """A host whose clock or timezone is behind would otherwise score a day
    whose bars do not exist yet, writing TIMEOUT on the whole book."""
    from intraday import outcomes
    sb = _CappedSB(_setups(5, day="2026-08-20"))
    with cfg_ctx({}), patch("kite.kite_client.get_kite", return_value=_ClockKite()):
        res = outcomes.resolve_day("2026-08-20", sb=sb, now=_ist("2026-08-14", 16, 0))
    assert res["resolved"] == 0, res
    assert res.get("reason") == "future_session", res


# ── 3. THE GUARD CAN BE CLEARED — the mirror rule ───────────────────────────

def test_resolve_day_scores_todays_session_after_the_close():
    from intraday import outcomes
    today = "2026-08-14"
    sb = _CappedSB(_setups(50, day=today))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        res = outcomes.resolve_day(today, sb=sb, now=_ist(today, 15, 40))
    assert res["resolved"] == 50, (
        f"resolved {res['resolved']} of 50 after the close — a guard that "
        f"cannot pass refuses the whole book forever")
    assert res.get("complete") is True, res


def test_the_daemons_own_cooldown_exit_clears_the_bar():
    """THE CASE THAT DECIDES WHETHER THIS FIX IS INERT OR HARMFUL.

    The daemon exits when `is_trading_session()` goes false — at COOLDOWN_TO,
    15:40. `finally` then calls resolve_day. If the guard's bar sat at or past
    that instant, the daemon could never score its own day again and every
    session would wait for the next evening's pipeline backfill. Assert
    against the SAME constants the daemon's exit reads, not against copies.
    """
    from intraday.config import COOLDOWN_TO, MARKET_CLOSE, is_trading_session
    from intraday import outcomes
    today = "2026-08-14"
    exit_at = _ist(today, COOLDOWN_TO.hour, COOLDOWN_TO.minute)
    assert not is_trading_session(exit_at + dt.timedelta(seconds=1)), (
        "COOLDOWN_TO is not when the daemon leaves the loop — this test is "
        "asserting against the wrong instant")
    ok, why = outcomes.session_is_over(today, now=exit_at)
    assert ok, (
        f"the daemon's own exit at {COOLDOWN_TO} is refused ({why}) — market "
        f"close is {MARKET_CLOSE}, so the buffer has swallowed the cool-down")
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        res = outcomes.resolve_day(today, sb=_CappedSB(_setups(3, day=today)),
                                   now=exit_at)
    assert res["resolved"] == 3, res


def test_the_buffer_cannot_be_configured_past_the_daemons_exit():
    """A key that can silently brick the scoring path is the silent-default
    failure this project keeps paying for. Clamped, and it must SAY so."""
    from intraday.config import COOLDOWN_TO
    from intraday import outcomes
    today = "2026-08-14"
    with cfg_ctx({"outcomes_close_buffer_min": "600"}):
        ok, _ = outcomes.session_is_over(
            today, now=_ist(today, COOLDOWN_TO.hour, COOLDOWN_TO.minute))
    assert ok, "a 600-minute buffer made the daemon's own exit unscorable"


def test_a_past_session_is_never_refused():
    """`backfill` only ever passes past dates. It must be untouched by this."""
    from intraday import outcomes
    ok, _ = outcomes.session_is_over(DAY, now=_ist("2026-08-15", 9, 30))
    assert ok
    ok, _ = outcomes.session_is_over(DAY, now=_ist("2026-08-15", 0, 1))
    assert ok, "a past session was refused at midnight — backfill is now blind"


def test_backfill_still_scores_every_past_session():
    from intraday import outcomes
    rows = _setups(30, day=DAY) + _setups(20, day="2026-08-13")
    sb = _CappedSB(rows)
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)), \
         patch("intraday.outcomes.today_ist",
               return_value=dt.date.fromisoformat("2026-08-16")):
        res = outcomes.backfill(sb=sb)
    assert res["resolved"] == 50, (
        f"backfill resolved {res['resolved']} of 50 — the guard is refusing "
        f"past sessions, which is the one thing it must never do")


# ── 4. PROVENANCE — which run scored which row ──────────────────────────────

def test_every_scored_row_records_when_and_by_which_run():
    from intraday import outcomes
    sb = _CappedSB(_migrated(_setups(3, day=DAY)))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        outcomes.resolve_day(DAY, sb=sb, now=_ist("2026-08-15", 9, 0))
    for p in sb.updates:
        assert p.get("scored_at"), f"no scored_at in {sorted(p)}"
        assert p.get("scored_by"), f"no scored_by in {sorted(p)}"


def test_scored_by_identifies_the_process_not_the_machine():
    """Two daemons on one host, or a daemon and the pipeline, are different
    RUNS. A hostname alone cannot separate the 42 STOP+TIMEOUT pairs."""
    from intraday import outcomes
    from intraday.lease import instance_id
    sb = _CappedSB(_migrated(_setups(1, day=DAY)))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        outcomes.resolve_day(DAY, sb=sb, now=_ist("2026-08-15", 9, 0))
    got = sb.updates[0]["scored_by"]
    assert got == instance_id(), got
    assert got.count("-") >= 2, (
        f"scored_by={got!r} carries no pid/uuid — two runs on one host would "
        f"be indistinguishable")


def test_provenance_is_dropped_whole_when_the_migration_is_unapplied():
    """PostgREST fails the WHOLE update on one unknown column. Code lands
    before its migration here routinely, and an outcome write lost because of
    a diagnostic column is a far worse bug than the one being diagnosed.

    The fixture here is the UNMIGRATED shape — no scored_* keys — which is
    what the live book actually returned on 16-Aug-2026.
    """
    from intraday import outcomes
    sb = _CappedSB(_setups(5, day=DAY))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        res = outcomes.resolve_day(DAY, sb=sb, now=_ist("2026-08-15", 9, 0))
    assert res["resolved"] == 5, (
        f"resolved {res['resolved']} of 5 with migration 082 unapplied — the "
        f"provenance columns are taking the outcome write down with them")
    for p in sb.updates:
        assert set(p) == {"outcome", "outcome_pct"}, sorted(p)


def test_a_half_applied_migration_is_treated_as_no_migration():
    """PostgREST is all-or-nothing on the statement, so two of three columns
    is not two thirds of a feature — it is a lost outcome write."""
    from intraday import outcomes
    rows = [{**r, "scored_at": None, "scored_by": None}
            for r in _setups(3, day=DAY)]          # scored_through absent
    sb = _CappedSB(rows)
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        res = outcomes.resolve_day(DAY, sb=sb, now=_ist("2026-08-15", 9, 0))
    assert res["resolved"] == 3, res
    for p in sb.updates:
        assert set(p) == {"outcome", "outcome_pct"}, sorted(p)


def test_the_schema_question_costs_no_extra_query():
    """A one-off `.select("scored_at,...)` probe would cost one call — and
    would name columns that do not exist in a SELECT list, which is precisely
    what tools/validate_selects.py exists to catch. It turned the `selects`
    health check RED, and a health check red for a known pending migration is
    how a real warning stops being read. The work queue already has the rows.
    """
    from intraday import outcomes
    sb = _CappedSB(_migrated(_setups(40, day=DAY)))
    with cfg_ctx({}), patch("kite.kite_client.get_kite",
                            return_value=_ClockKite(end=(15, 29), close=101.0)):
        outcomes.resolve_day(DAY, sb=sb, now=_ist("2026-08-15", 9, 0))
    reads = [e for e in sb.log if e[0] in ("unranged", "range")]
    assert len(reads) == 1, (
        f"{len(reads)} reads of intraday_setups for one date — the schema "
        f"question is costing a query it does not need to")


def test_the_work_queue_still_selects_star():
    """_provenance_supported() reads the KEYS of a work-queue row, so it is
    correct only while that read is unprojected. Narrowing the select to a
    column list would silently switch provenance off on a migrated book —
    no error, no warning, just three columns quietly never written again."""
    import inspect
    from intraday import outcomes
    src = inspect.getsource(outcomes.resolve_day)
    assert '.select("*")' in src, (
        "resolve_day's work queue no longer selects * — _provenance_supported "
        "reads row keys and will now report every column missing")


def test_provenance_names_exactly_what_migration_082_adds():
    """The column list in code and the column list in the migration are two
    claims about one schema. Asserted against the migration FILE, because a
    typo in either is invisible until a live write silently drops."""
    from pathlib import Path
    from intraday.outcomes import PROVENANCE_COLS
    sql = (Path(__file__).resolve().parent.parent / "db" / "migrations"
           / "082_outcome_scoring_provenance.sql").read_text(encoding="utf-8")
    for col in PROVENANCE_COLS:
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql, (
            f"code writes {col} and migration 082 does not add it")


TESTS = [
    ("the clock fake ends where it is told to",
     test_the_clock_fake_ends_where_it_is_told_to),
    ("the clock fake prices a different close at a different end",
     test_the_clock_fake_prices_a_different_close_at_a_different_end),
    ("a truncated series prices a TIMEOUT at the wrong close",
     test_a_truncated_series_prices_a_timeout_at_the_wrong_close),
    ("the stored row says which bar priced it",
     test_the_stored_row_says_which_bar_priced_it),
    ("resolve_day refuses today's session before the close",
     test_resolve_day_refuses_todays_session_before_the_close),
    ("the refusal writes nothing at all", test_the_refusal_writes_nothing_at_all),
    ("a refusal is not mistakable for a finished day",
     test_a_refusal_is_not_mistakable_for_a_finished_day),
    ("the daemon's default call is the guarded one",
     test_the_daemons_default_call_is_the_guarded_one),
    ("a future date is refused", test_a_future_date_is_refused),
    ("resolve_day scores today's session after the close",
     test_resolve_day_scores_todays_session_after_the_close),
    ("the daemon's own cool-down exit clears the bar",
     test_the_daemons_own_cooldown_exit_clears_the_bar),
    ("the buffer cannot be configured past the daemon's exit",
     test_the_buffer_cannot_be_configured_past_the_daemons_exit),
    ("a past session is never refused", test_a_past_session_is_never_refused),
    ("backfill still scores every past session",
     test_backfill_still_scores_every_past_session),
    ("every scored row records when and by which run",
     test_every_scored_row_records_when_and_by_which_run),
    ("scored_by identifies the process, not the machine",
     test_scored_by_identifies_the_process_not_the_machine),
    ("provenance is dropped whole when the migration is unapplied",
     test_provenance_is_dropped_whole_when_the_migration_is_unapplied),
    ("a half-applied migration is treated as no migration",
     test_a_half_applied_migration_is_treated_as_no_migration),
    ("the schema question costs no extra query",
     test_the_schema_question_costs_no_extra_query),
    ("the work queue still selects *", test_the_work_queue_still_selects_star),
    ("provenance names exactly what migration 082 adds",
     test_provenance_names_exactly_what_migration_082_adds),
]
