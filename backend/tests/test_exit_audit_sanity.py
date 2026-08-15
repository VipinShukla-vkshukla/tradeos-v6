"""
max_favorable_excursion/max_adverse_excursion are PERCENTAGES, already
direction-signed — not prices (10-Aug-2026, third pass, root cause found).

WHAT THIS CATCHES
-----------------
Two earlier passes at this bug both guessed at the cause and were wrong. The
first assumed a near-zero `planned_stop_at_entry` (a data problem on specific
rows); re-run against the real book, the SAME rows produced the SAME corrupted
numbers, unchanged to three decimals, proving that theory false. The second
added a symptom-based magnitude cap and printed raw values instead of guessing
again — and the raw values solved it immediately: KIMS showed entry=802.50,
mfe=2.15. A value of 2.15 next to an entry of 802.50 cannot be a price.

Confirmed against the WRITER, not just the symptom:
`intraday/engine.py::_track_excursion` computes
`pct = D.gain_pct(entry, ltp, d)` and stores `max(prior, pct)` — a PERCENTAGE,
and `D.gain_pct` is already direction-adjusted ("signed in the position's
favour", its own docstring). This tool's old formula treated the column as an
absolute price AND re-applied a direction flip to a value already flipped
once — wrong twice over. Correct formula: `mfe_r = mfe_pct / risk_pct`, no
direction branch, because the sign is already correct however the position
was structured.

Pinned against the operator's own real, previously-misread rows so a
regression here is caught against production shapes, not just synthetic ones.
"""

from __future__ import annotations

import io

from loguru import logger


class _Q:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def order(self, col, *a, **k):
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        LIMIT/OFFSET with no ORDER BY has no stable row order between
        requests, so pages repeat rows and drop others — 8324 matching rows
        came back as 5000 distinct on the live book. A fake without this
        method does not fail loudly: the AttributeError is swallowed by the
        caller's non-fatal except, or turns a paged fetch into an empty list,
        which is how test_setup_rehydration silently lost 4 of 7 checks.
        """
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, a, b): return _E(self._rows[a:b + 1])


class _E:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _Q(self._rows)


def _row(entry, stop, mfe_pct, mae_pct, r_mult, reason="TARGET_HIT",
        direction="LONG", fw="SWING", symbol="X"):
    """mfe_pct/mae_pct are PERCENTAGES, already signed in the trade's favour —
    matching what intraday/engine.py::_track_excursion actually writes."""
    return {"symbol": symbol, "framework": fw, "direction": direction,
            "entry_price": entry, "exit_price": None, "r_multiple": r_mult,
            "exit_reason": reason, "max_favorable_excursion": mfe_pct,
            "max_adverse_excursion": mae_pct, "planned_stop_at_entry": stop,
            "realized_pnl": None, "charges": None}


def _captured_logs(fn) -> str:
    sink = io.StringIO()
    hid = logger.add(sink, level="WARNING")
    try:
        fn()
    finally:
        logger.remove(hid)
    return sink.getvalue()


def test_a_near_zero_stop_is_still_excluded_from_excursion_stats():
    """The FIRST guard (risk-denominator) still matters on its own terms —
    a stop 0.06% from entry is not a real stop for either book, whatever the
    excursion columns say."""
    from tools.exit_audit import audit_closed
    rows = [_row(1554.80, 1553.80, 5.4, -0.3, r_mult=0.49)]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "excluded from capture" in out.lower() or "1 closed trade" in out


def test_realised_r_survives_exclusion():
    """Realised R must not depend on the excursion guard at all — it is read
    from a different column, computed at close time."""
    from tools.exit_audit import _f
    rows = [_row(1554.80, 1553.80, 5.4, -0.3, r_mult=0.49)]
    recs = [_f(r.get("r_multiple")) for r in rows]
    assert recs == [0.49]


def test_kims_the_row_that_exposed_the_bug():
    """The operator's own real, previously-misread row. Under the OLD (price)
    formula this produced mfe_r=-36.46. Under the CORRECT (percentage)
    formula it must land near +0.786R — a completely ordinary swing
    excursion, comfortably inside MAX_PLAUSIBLE_R, and NOT flagged."""
    from tools.exit_audit import audit_closed
    rows = [_row(802.50, 780.55, 2.15, -0.673, r_mult=0.487,
                reason="BROKER_EXIT", symbol="KIMS")]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "KIMS" not in out, f"a sane real trade was flagged as implausible: {out!r}"
    assert "STILL produced" not in out


def test_bhel_a_second_real_row_with_a_wide_stop():
    """entry=407.40, stop=383.89 (5.77% risk), mfe=0.466%, mae=-0.098%. Under
    the correct formula this is a barely-moved position (~0.08R peak) against
    a wide stop -- ordinary, not implausible."""
    from tools.exit_audit import audit_closed
    rows = [_row(407.40, 383.89, 0.466, -0.098, r_mult=0.487,
                reason="BROKER_EXIT", symbol="BHEL")]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "BHEL" not in out, f"a sane real trade was flagged as implausible: {out!r}"


def test_coforge_a_real_short_shaped_row():
    """entry=1769.90, stop=1794.45 (stop ABOVE entry -- a short). mfe=0.209%,
    mae=-0.701%, both ALREADY signed in the trade's favour per D.gain_pct's
    own convention -- no direction branch needed or wanted here."""
    from tools.exit_audit import audit_closed
    rows = [_row(1769.90, 1794.45, 0.209, -0.701, r_mult=0.1,
                direction="SHORT", fw="INTRADAY", symbol="COFORGE")]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "INTRADAY"))
    assert "COFORGE" not in out, f"a sane real short was flagged as implausible: {out!r}"


def test_a_genuinely_absurd_percentage_is_still_caught():
    """The safety net must still fire on data that is absurd EVEN under the
    correct percentage interpretation -- a 250% favourable excursion is not
    reachable by any real position, price-unit confusion or not."""
    from tools.exit_audit import audit_closed
    rows = [_row(100.0, 97.0, 250.0, -95.0, r_mult=0.3)]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "STILL produced" in out and "over 15.0" in out, (
        f"a genuinely absurd percentage was not flagged: {out!r}")


def test_raw_values_are_printed_for_implausible_rows():
    """Diagnosis, not just detection -- the raw percentage values must be
    visible, since that is exactly what solved this bug the first time."""
    from tools.exit_audit import audit_closed
    rows = [_row(100.0, 97.0, 250.0, -95.0, r_mult=0.3)]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "mfe_pct=250" in out, f"raw percentage values missing from output: {out!r}"


TESTS = [
    ("a near-zero stop is still excluded from excursion stats",
     test_a_near_zero_stop_is_still_excluded_from_excursion_stats),
    ("realised R survives exclusion",
     test_realised_r_survives_exclusion),
    ("KIMS -- the row that exposed the bug",
     test_kims_the_row_that_exposed_the_bug),
    ("BHEL -- a second real row with a wide stop",
     test_bhel_a_second_real_row_with_a_wide_stop),
    ("COFORGE -- a real short-shaped row",
     test_coforge_a_real_short_shaped_row),
    ("a genuinely absurd percentage is still caught",
     test_a_genuinely_absurd_percentage_is_still_caught),
    ("raw values are printed for implausible rows",
     test_raw_values_are_printed_for_implausible_rows),
]
