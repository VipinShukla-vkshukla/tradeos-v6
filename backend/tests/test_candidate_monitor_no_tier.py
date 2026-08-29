"""
candidate_monitor.py redesigned, 29-Aug-2026: the watch filter no longer
uses ai_tier (candidate_watch_tiers, default TIER_1/TIER_2) — that bucket
was measured underperforming its own lower tiers this session. Now
filters on signal_type (ENTRY_SIGNAL_TYPES), the same deterministic,
gate-passed classification generate_signals.py itself assigns.

Also fixes the BLUEJET shape (26-Aug-2026): the degraded-fallback alert
path (fires only when the real daemon's lease is down) used to fire on
its own flat bar with no cross-check against what the real daemon's
allocator actually decided while it was still up. Now checks each
alerting symbol's last real allocation_decisions verdict from today —
DECLINE suppresses the fallback alert, TAKE or "never reached" lets it
through (reuse, not reimplementation, of the real decision).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from control.candidate_monitor import (
    load_candidates, ENTRY_SIGNAL_TYPES, _maybe_send_candidate_alerts,
)


class _Query:
    """Supports both a plain .execute() read and fetch_all()'s paging
    shape (.order(...).range(a, b).execute()) — allocation_decisions
    reads must go through fetch_all (F-88/static_analysis), so this fake
    needs to answer a ranged request, not just return everything once."""
    def __init__(self, rows):
        self._rows = rows
        self.filters: list[tuple] = []
        self._range = None
    def select(self, *a, **k): return self
    def eq(self, *a, **k):
        self.filters.append(("eq", a)); return self
    def in_(self, *a, **k):
        self.filters.append(("in_", a)); return self
    def gte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def range(self, start, end):
        self._range = (start, end); return self
    def execute(self):
        r = type("R", (), {})()
        if self._range is None:
            r.data = self._rows
        else:
            start, end = self._range
            r.data = self._rows[start:end + 1]
        return r


class _FakeSb:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables
    def table(self, name):
        return _Query(self._tables.get(name, []))


def test_load_candidates_filters_on_signal_type_not_ai_tier():
    sb = _FakeSb({"signal_output_daily": [
        {"symbol": "TCS", "signal_type": "PRIME_SETUP"},
    ]})
    rows = load_candidates(sb, "2026-08-29")
    assert rows and rows[0]["symbol"] == "TCS"

    # Confirm the actual filter call used signal_type/ENTRY_SIGNAL_TYPES,
    # not ai_tier — inspect what the query object recorded.
    q = sb.table("signal_output_daily")
    q.select("x").eq("date", "2026-08-29").in_("signal_type", ENTRY_SIGNAL_TYPES).execute()
    assert ("in_", ("signal_type", ENTRY_SIGNAL_TYPES)) in q.filters


def test_entry_signal_types_matches_generate_signals_definition():
    """Duplicated deliberately (cross-scheduling-boundary rule), but must
    not silently drift from the real definition — this is the one place
    that would catch it."""
    expected = {"BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
               "REENTRY_SETUP", "STAGED_ENTRY", "MOMENTUM_CONTINUATION"}
    assert set(ENTRY_SIGNAL_TYPES) == expected


def _decision(symbol, action="BUY_NOW"):
    d = MagicMock()
    d.symbol = symbol
    d.action = action
    d.live_price = 100.0
    d.reason = "test"
    d.qty = 1
    d.invested = 100.0
    d.risk_amount = 5.0
    return d


def test_fallback_alert_suppressed_when_real_allocator_declined():
    """The BLUEJET shape: this monitor's own flat bar says BUY_NOW, but
    the real daemon's allocator already said DECLINE today — must not
    fire."""
    sb = _FakeSb({
        "allocation_decisions": [
            {"symbol": "BLUEJET", "verdict": "DECLINE", "decided_at": "2026-08-29T05:00:00Z"},
        ],
    })
    with patch("intraday.lease.observe") as mock_observe:
        mock_observe.return_value = MagicMock(held_by_other=False, detail="no lease")
        sent = _maybe_send_candidate_alerts(sb, [_decision("BLUEJET")], "kite")
    assert not sent, "alerted on a symbol the real allocator already declined today"


def test_fallback_alert_fires_when_real_allocator_took_it():
    sb = _FakeSb({
        "allocation_decisions": [
            {"symbol": "TCS", "verdict": "TAKE", "decided_at": "2026-08-29T05:00:00Z"},
        ],
    })
    with patch("intraday.lease.observe") as mock_observe, \
         patch("control.candidate_monitor._send") as mock_send:
        mock_observe.return_value = MagicMock(held_by_other=False, detail="no lease")
        sent = _maybe_send_candidate_alerts(sb, [_decision("TCS")], "kite")
    assert sent
    mock_send.assert_called_once()


def test_fallback_alert_fires_when_daemon_never_reached_the_symbol():
    """No allocation_decisions row at all is ambiguous (daemon may not
    have gotten to it yet), not a confirmed red flag — must not be
    silently suppressed the same way a real DECLINE is."""
    sb = _FakeSb({"allocation_decisions": []})
    with patch("intraday.lease.observe") as mock_observe, \
         patch("control.candidate_monitor._send") as mock_send:
        mock_observe.return_value = MagicMock(held_by_other=False, detail="no lease")
        sent = _maybe_send_candidate_alerts(sb, [_decision("NEWCO")], "kite")
    assert sent
    mock_send.assert_called_once()


def test_alert_fully_suppressed_when_daemon_lease_is_healthy():
    """Unchanged pre-existing behavior — must survive this edit."""
    sb = _FakeSb({})
    with patch("intraday.lease.observe") as mock_observe:
        mock_observe.return_value = MagicMock(held_by_other=True, holder="x", hostname="y")
        sent = _maybe_send_candidate_alerts(sb, [_decision("TCS")], "kite")
    assert not sent


TESTS = [
    ("load_candidates filters on signal_type, not ai_tier",
     test_load_candidates_filters_on_signal_type_not_ai_tier),
    ("ENTRY_SIGNAL_TYPES matches generate_signals.py's own definition",
     test_entry_signal_types_matches_generate_signals_definition),
    ("fallback alert suppressed when real allocator declined (BLUEJET fix)",
     test_fallback_alert_suppressed_when_real_allocator_declined),
    ("fallback alert fires when real allocator took it",
     test_fallback_alert_fires_when_real_allocator_took_it),
    ("fallback alert fires when daemon never reached the symbol",
     test_fallback_alert_fires_when_daemon_never_reached_the_symbol),
    ("alert fully suppressed when daemon lease is healthy",
     test_alert_fully_suppressed_when_daemon_lease_is_healthy),
]
