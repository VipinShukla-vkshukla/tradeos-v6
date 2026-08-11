"""
Six shorts refused for runway in one afternoon (AEGISLOG, CEMPRO, CHOLAFIN,
DATAPATTNS, ENRIN, SONACOMS, 11-Aug-2026) may still be right — the setup just
matured too late in the session to act on. requeue_runway_refused_shorts()
re-seeds the ones that gapped down again at the NEXT day's open, when a fresh
short obviously clears the 75-minute runway, back into that day's universe —
through the FULL pipeline again, never as a shortcut.

Two pure functions under direct test (_parse_runway_refusals,
_gap_continuation_shorts), plus the orchestration method's own gating
(default off touches no database; OPENING-phase-only; once per session).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from tests import cfg_ctx
from config import IST


# ── _parse_runway_refusals(): tell runway apart from circuit/squeeze/liquidity

def test_parse_keeps_only_cover_deadline_refusals():
    from intraday.engine import _parse_runway_refusals
    rows = [
        {"symbol": "AEGISLOG", "meta": json.dumps({
            "shortability_reason": "40 min to the cover deadline, and a short "
                                   "needs 75 — an uncovered short goes to auction"})},
        {"symbol": "SOMENAME", "meta": json.dumps({
            "shortability_reason": "delivers 82% of its volume against a 75% "
                                   "ceiling — squeeze candidate"})},
        {"symbol": "OTHERNAME", "meta": json.dumps({
            "shortability_reason": "trades Rs 10cr against a Rs 50cr floor"})},
    ]
    out = _parse_runway_refusals(rows)
    assert [r["symbol"] for r in out] == ["AEGISLOG"], out


def test_parse_handles_meta_already_as_a_dict():
    """Defensive: some callers may hand back parsed JSON, not a string."""
    from intraday.engine import _parse_runway_refusals
    rows = [{"symbol": "AEGISLOG",
            "meta": {"shortability_reason": "min to the cover deadline"}}]
    out = _parse_runway_refusals(rows)
    assert len(out) == 1


def test_parse_ignores_malformed_meta_without_raising():
    from intraday.engine import _parse_runway_refusals
    rows = [{"symbol": "AEGISLOG", "meta": "{not json"},
            {"symbol": "CEMPRO", "meta": None}]
    assert _parse_runway_refusals(rows) == []


# ── _gap_continuation_shorts(): only continuation gets re-seeded ────────────

def test_gapped_down_symbol_is_reseeded():
    from intraday.engine import _gap_continuation_shorts
    rows = [{"symbol": "AEGISLOG"}]
    out = _gap_continuation_shorts(rows, {"AEGISLOG": 100.0}, {"AEGISLOG": 97.5})
    assert out == ["AEGISLOG"]


def test_flat_or_up_open_is_not_reseeded():
    from intraday.engine import _gap_continuation_shorts
    rows = [{"symbol": "CEMPRO"}, {"symbol": "CHOLAFIN"}]
    out = _gap_continuation_shorts(
        rows, {"CEMPRO": 100.0, "CHOLAFIN": 100.0},
        {"CEMPRO": 100.0, "CHOLAFIN": 101.0})   # flat, then up
    assert out == []


def test_missing_price_on_either_side_is_skipped_not_guessed():
    from intraday.engine import _gap_continuation_shorts
    rows = [{"symbol": "ENRIN"}, {"symbol": "SONACOMS"}]
    out = _gap_continuation_shorts(
        rows, {"ENRIN": 100.0}, {"SONACOMS": 95.0})  # neither symbol has both
    assert out == []


def test_duplicate_rows_for_the_same_symbol_are_not_double_counted():
    from intraday.engine import _gap_continuation_shorts
    rows = [{"symbol": "AEGISLOG"}, {"symbol": "AEGISLOG"}]
    out = _gap_continuation_shorts(rows, {"AEGISLOG": 100.0}, {"AEGISLOG": 97.0})
    assert out == ["AEGISLOG"]


# ── requeue_runway_refused_shorts(): the orchestration gating ───────────────

class _NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_NoDB())


def test_default_off_touches_no_database():
    with cfg_ctx():
        eng = _engine()
        assert eng.requeue_runway_refused_shorts() == []


def test_off_hours_does_not_touch_database_even_when_armed():
    """Armed, but it is 11:00 (PRIME), well outside the OPENING window."""
    with cfg_ctx({"intraday_requeue_runway_refused": "true"}):
        eng = _engine()
        import intraday.engine as engine_mod
        orig = engine_mod.datetime

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 12, 11, 0, tzinfo=IST)

        engine_mod.datetime = _Frozen
        try:
            assert eng.requeue_runway_refused_shorts() == []
        finally:
            engine_mod.datetime = orig


def test_runs_once_per_session_even_if_called_again_in_the_opening_window():
    """Second call the same day must not re-query, even while still armed
    and still inside OPENING — the dedup key must be set on the FIRST call
    before any database access, not after a successful one."""
    from config import today_ist
    with cfg_ctx({"intraday_requeue_runway_refused": "true"}):
        eng = _engine()
        import intraday.engine as engine_mod
        orig = engine_mod.datetime
        today = today_ist()  # whatever "today" actually resolves to for this run

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(today.year, today.month, today.day, 9, 20, tzinfo=IST)

        engine_mod.datetime = _Frozen
        try:
            # Set to the SAME value today_ist().isoformat() will produce
            # inside the method, so this exercises the dedup match exactly —
            # not a coincidentally-matching hardcoded date.
            eng._last_state["_runway_requeue_date"] = today.isoformat()
            assert eng.requeue_runway_refused_shorts() == [], (
                "must short-circuit on the dedup key before touching _NoDB")
        finally:
            engine_mod.datetime = orig


TESTS = [
    ("parse keeps only cover-deadline refusals", test_parse_keeps_only_cover_deadline_refusals),
    ("parse handles meta already as a dict", test_parse_handles_meta_already_as_a_dict),
    ("parse ignores malformed meta without raising", test_parse_ignores_malformed_meta_without_raising),
    ("gapped-down symbol is reseeded", test_gapped_down_symbol_is_reseeded),
    ("flat or up open is not reseeded", test_flat_or_up_open_is_not_reseeded),
    ("missing price on either side is skipped, not guessed",
     test_missing_price_on_either_side_is_skipped_not_guessed),
    ("duplicate rows for the same symbol are not double-counted",
     test_duplicate_rows_for_the_same_symbol_are_not_double_counted),
    ("default off touches no database", test_default_off_touches_no_database),
    ("off-hours does not touch database even when armed",
     test_off_hours_does_not_touch_database_even_when_armed),
    ("runs once per session even if called again in the opening window",
     test_runs_once_per_session_even_if_called_again_in_the_opening_window),
]
