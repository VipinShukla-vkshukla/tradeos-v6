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


TESTS = [
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
