"""
range_verdict() and vwap_verdict() are the ONE definition of "does quote mode
still agree with reality", shared by tools.quote_parity.report() (the CLI,
read by a human) and tools.health.check_quote_parity() (the ongoing check run
before every trading session). If these two ever disagreed about what counts
as a fault, a switch could read "safe" from one tool and "unsafe" from the
other on the exact same data — this is what stops that from being possible:
one pure function, two callers, tested here without either caller.

WHY THIS EXISTS — 08-Aug-2026
------------------------------
The first version of the quote-mode switches was measured once (07-Aug-2026),
and the tool that measured it told the operator to disarm logging once it
read clean — freezing that day's verdict with nothing left running to notice
a later session disagreeing. These two functions are what a continuously-
running check (tools.health) is built on, so "is this still true" is a
question the system can answer on any later day, not just the one it was
first asked.
"""

from __future__ import annotations

from tests import cfg_ctx


def _row(field: str, diff_pct: float) -> dict:
    return {"field": field, "diff_pct": diff_pct}


def test_range_verdict_none_when_no_range_rows():
    from tools.quote_parity import range_verdict
    rows = [_row("vwap", -0.39), _row("prev_close", -4.18)]
    ok, detail = range_verdict(rows)
    assert ok is None, f"expected None with no day_high/day_low rows, got {ok}"


def test_range_verdict_clean():
    from tools.quote_parity import range_verdict
    rows = [_row("day_high", 0.01), _row("day_high", 0.03),
            _row("day_low", -0.01), _row("day_low", 0.0)]
    ok, detail = range_verdict(rows)
    assert ok is True, f"expected clean range to verify OK, got {ok}: {detail}"


def test_range_verdict_catches_day_high_behind():
    from tools.quote_parity import range_verdict
    rows = [_row("day_high", 0.01), _row("day_high", -0.20),  # live BEHIND fetched
            _row("day_low", 0.0)]
    ok, detail = range_verdict(rows)
    assert ok is False, f"a live day_high behind the fetched one must fault, got {ok}"
    assert "1 of" in detail


def test_range_verdict_catches_day_low_behind():
    from tools.quote_parity import range_verdict
    rows = [_row("day_high", 0.0), _row("day_low", 0.20)]  # live BEHIND (above) fetched
    ok, detail = range_verdict(rows)
    assert ok is False, f"a live day_low behind the fetched one must fault, got {ok}"


def test_range_verdict_ignores_vwap_and_prev_close():
    """vwap/prev_close being terrible must not affect the range verdict at all."""
    from tools.quote_parity import range_verdict
    rows = [_row("day_high", 0.0), _row("day_low", 0.0),
            _row("vwap", -5.0), _row("prev_close", -10.0)]
    ok, detail = range_verdict(rows)
    assert ok is True, f"vwap/prev_close faults must not leak into range's verdict, got {ok}"
    assert "2 day_high/day_low" in detail


def test_vwap_verdict_none_when_no_vwap_rows():
    from tools.quote_parity import vwap_verdict
    with cfg_ctx({}):
        ok, detail = vwap_verdict([_row("day_high", 0.0)])
        assert ok is None, f"expected None with no vwap rows, got {ok}"


def test_vwap_verdict_clean_when_inside_every_engine_tolerance():
    from tools.quote_parity import vwap_verdict
    with cfg_ctx({}):
        # tightest default tolerance is vwr_stop_buffer_pct at 0.08% — stay under it
        rows = [_row("vwap", 0.01), _row("vwap", -0.02), _row("vwap", 0.00)]
        ok, detail = vwap_verdict(rows)
        assert ok is True, f"diffs inside every tolerance must verify OK, got {ok}: {detail}"


def test_vwap_verdict_faults_on_the_measured_shape():
    """Matches the actual 07-Aug-2026 measurement: mostly clean, one -0.39% outlier."""
    from tools.quote_parity import vwap_verdict
    with cfg_ctx({}):
        rows = [_row("vwap", 0.0)] * 100 + [_row("vwap", -0.39)]
        ok, detail = vwap_verdict(rows)
        assert ok is False, (
            f"a -0.39% outlier exceeds vwr_stop_buffer_pct's default 0.08% tolerance "
            f"and must fault, got {ok}: {detail}")


def test_vwap_verdict_respects_a_live_tolerance_override():
    """
    If the operator widens the tightest engine tolerance past the measured
    gap, the same data that faulted above must read OK — the function reads
    config live (cfg_float), it does not hardcode the default.
    """
    from tools.quote_parity import vwap_verdict
    with cfg_ctx({"vwr_stop_buffer_pct": "1.0",
                  "intraday_short_stop_buffer_pct": "1.0",
                  "pbk_stop_buffer_pct": "1.0",
                  "pbk_touch_tol_pct": "1.0",
                  "intraday_short_vwap_near_pct": "1.0",
                  "vwr_max_extension_pct": "1.0"}):
        rows = [_row("vwap", 0.0)] * 100 + [_row("vwap", -0.39)]
        ok, detail = vwap_verdict(rows)
        assert ok is True, (
            f"with every engine tolerance widened past 0.39%, this must verify OK, "
            f"got {ok}: {detail}")


# ── The 17-Aug-2026 fault, and the two defects behind it ────────────────────
#
# health's quote_parity check read RANGE REGRESSED. It had not regressed; the
# bar side had picked up the pre-open call auction, whose prints carry the
# PREVIOUS CLOSE as last_price until an equilibrium price is published. A
# max/min over such a series returns yesterday's close instead of today's
# extreme, in whichever direction the stock gapped.
#
# It read "clean" for ten days because the daemon happened to start late on
# every day that read clean (07-Aug 10:08, 10-Aug 09:41, 11-Aug 09:30, 13-Aug
# 09:52) and at 09:21 on all three days that faulted. The check was defending
# a baseline measured an hour after the window the defect lives in.


def _tick_ts(hh: int, mm: int):
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime(2026, 8, 17, hh, mm, 0, tzinfo=IST)


def test_pre_open_ticks_do_not_become_bars():
    """
    The exact 17-Aug shape: BELRISE ticked at its 14-Aug close of 255.35
    through the pre-open, then opened at 247.77 and never traded higher. A
    day_high taken over the tick-built series must be 247.77, not 255.35.
    """
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    for m in range(0, 15):                      # 09:00 .. 09:14, pre-open
        b.record_tick("BELRISE", 255.35, _tick_ts(9, m))
    for m in range(15, 27):                     # 09:15 .. 09:26, real session
        b.record_tick("BELRISE", 247.77 - (m - 15) * 0.5, _tick_ts(9, m))

    bars = b.closed_bars("BELRISE")
    assert bars, "the session ticks must still build bars"
    high = max(x.high for x in bars)
    assert high == 247.77, (
        f"day_high over tick-built bars must be the session high 247.77, got {high} "
        f"— a pre-open print at the previous close reached the bar series")
    assert all(x.ts.hour * 60 + x.ts.minute >= 9 * 60 + 15 for x in bars), \
        "no bar may be stamped before 09:15"


def test_post_close_ticks_do_not_become_bars():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    for m in (20, 25, 29):
        b.record_tick("SBIN", 1060.0, _tick_ts(15, m))
    b.record_tick("SBIN", 1099.0, _tick_ts(15, 35))   # closing session
    b.record_tick("SBIN", 1099.0, _tick_ts(15, 38))
    bars = b.closed_bars("SBIN")
    high = max(x.high for x in bars) if bars else None
    assert high == 1060.0, (
        f"a closing-session print must not extend the day range, got {high}")


def test_day_rollover_still_clears_on_a_pre_open_tick():
    """
    The session filter must not be allowed to skip the day-rollover reset. The
    socket is subscribed from 09:00, so if an out-of-session tick returned
    before the reset, closed_bars() would serve YESTERDAY's session to anything
    reading it between 09:00 and 09:15 — worse than the bug being fixed.
    """
    from datetime import datetime, timezone, timedelta
    from intraday.bar_builder import BarBuilder
    IST = timezone(timedelta(hours=5, minutes=30))
    b = BarBuilder()
    for m in (20, 25, 29):
        b.record_tick("SBIN", 900.0, datetime(2026, 8, 14, 15, m, tzinfo=IST))
    assert b.closed_bars("SBIN"), "precondition: yesterday built bars"

    b.record_tick("SBIN", 1067.7, _tick_ts(9, 3))     # pre-open, next day
    assert not b.closed_bars("SBIN"), (
        "a new session's first tick must clear yesterday's bars even when the "
        "tick itself is out of session")


def test_parity_logs_against_the_fetched_snapshot_not_the_overlaid_field():
    """
    The overlay writes the tick value into ctx.day_high, which is what the
    parity logger used to read — so it compared the feed to itself. The
    snapshot must survive an overlay untouched.
    """
    from intraday.strategies.base import Bar, SymbolContext
    from intraday.engine import _fetched_snapshot
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    bars = [Bar(datetime(2026, 8, 17, 9, 15, tzinfo=IST), 100.0, 101.0, 99.0, 100.5, 10.0)]
    ctx = SymbolContext(symbol="X", ltp=100.5, bars=bars, day_high=101.0,
                        fetched=_fetched_snapshot(bars, 100.2, 98.0))

    ctx.day_high = 103.0            # what _overlay() does with the tick value
    assert ctx.fetched["day_high"] == 101.0, (
        "the overlay must not be able to reach the value parity compares against")
    assert ctx.fetched["prev_close"] == 98.0
    assert ctx.fetched["volume"] == 10.0


def test_fetched_snapshot_tracks_bars_so_it_cannot_freeze():
    """
    A bench-only context is never rebuilt — refresh_contexts() only rebuilds
    the historical universe — so a snapshot taken once at build time would
    report a fault every cycle for the rest of the day as the real range moved
    away from it.
    """
    from intraday.strategies.base import Bar
    from intraday.engine import _fetched_snapshot
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    early = [Bar(datetime(2026, 8, 17, 9, 15, tzinfo=IST), 100.0, 101.0, 99.0, 100.5, 10.0)]
    later = early + [Bar(datetime(2026, 8, 17, 10, 15, tzinfo=IST),
                         100.5, 108.0, 100.0, 107.0, 12.0)]
    assert _fetched_snapshot(early, None, None)["day_high"] == 101.0
    assert _fetched_snapshot(later, None, None)["day_high"] == 108.0, \
        "the snapshot must move with the bars it is derived from"


def test_parity_logs_exactly_the_fields_quote_parity_scores():
    """
    LOGGED and the snapshot are two different modules' opinions about which
    fields exist. day_open is deliberately in the snapshot and NOT logged;
    everything scored must be logged, or a fault would be unobservable.
    """
    from tools.quote_parity import LOGGED, SCORED
    from intraday.engine import _fetched_snapshot
    snap = _fetched_snapshot([], None, None)
    missing = [f for f in LOGGED if f not in snap]
    assert not missing, f"LOGGED names fields the snapshot does not carry: {missing}"
    unscored = [f for f in SCORED if f not in LOGGED]
    assert not unscored, f"scored but never logged, so it can never fault: {unscored}"


TESTS = [
    ("pre-open ticks do not become bars", test_pre_open_ticks_do_not_become_bars),
    ("post-close ticks do not become bars", test_post_close_ticks_do_not_become_bars),
    ("day rollover still clears on a pre-open tick", test_day_rollover_still_clears_on_a_pre_open_tick),
    ("parity reads the snapshot, not the overlaid field", test_parity_logs_against_the_fetched_snapshot_not_the_overlaid_field),
    ("the fetched snapshot cannot freeze", test_fetched_snapshot_tracks_bars_so_it_cannot_freeze),
    ("every scored field is actually logged", test_parity_logs_exactly_the_fields_quote_parity_scores),
    ("range_verdict is None with no range rows", test_range_verdict_none_when_no_range_rows),
    ("range_verdict clean case", test_range_verdict_clean),
    ("range_verdict catches day_high behind", test_range_verdict_catches_day_high_behind),
    ("range_verdict catches day_low behind", test_range_verdict_catches_day_low_behind),
    ("range_verdict ignores vwap/prev_close", test_range_verdict_ignores_vwap_and_prev_close),
    ("vwap_verdict is None with no vwap rows", test_vwap_verdict_none_when_no_vwap_rows),
    ("vwap_verdict clean inside every tolerance", test_vwap_verdict_clean_when_inside_every_engine_tolerance),
    ("vwap_verdict faults on the measured shape", test_vwap_verdict_faults_on_the_measured_shape),
    ("vwap_verdict respects a live tolerance override", test_vwap_verdict_respects_a_live_tolerance_override),
]
