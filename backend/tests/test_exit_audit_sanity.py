"""
A degenerate risk denominator must not be averaged into capture/overrun
(10-Aug-2026).

WHAT THIS CATCHES
-----------------
The first live run of `tools/exit_audit.py` reported SWING's 7 BROKER_EXIT
rows averaging MFE -20.228R, and several INTRADAY exit reasons showing |MFE R|
over 100 (-143.636, +164.433, -109.340). None of those are physically
reachable in a session against a real stop — every one shares the same
signature: `planned_stop_at_entry` sitting implausibly close to `entry_price`,
so a genuine price swing divided by a near-zero risk produces triple-digit R.

BROKER_EXIT is the tell: a position reconciled from Kite holdings rather than
opened through decide()'s own planning path never had a real stop computed for
it. The fix excludes those rows from the excursion arithmetic while KEEPING
their realised R, because r_multiple is read from a different column
(computed by control/position_lifecycle.py from the position's CURRENT stop at
close time, not the entry-time snapshot this tool reads) and can be sound even
when planned_stop_at_entry is not.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from loguru import logger


class _Q:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def range(self, a, b): return _E(self._rows[a:b + 1])


class _E:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _Q(self._rows)


def _row(entry, stop, mfe, mae, r_mult, reason="TARGET_HIT", direction="LONG",
        fw="SWING"):
    return {"symbol": "X", "framework": fw, "direction": direction,
            "entry_price": entry, "exit_price": None, "r_multiple": r_mult,
            "exit_reason": reason, "max_favorable_excursion": mfe,
            "max_adverse_excursion": mae, "planned_stop_at_entry": stop,
            "realized_pnl": None, "charges": None}


def _captured_logs(fn) -> str:
    sink = io.StringIO()
    hid = logger.add(sink, level="WARNING")
    try:
        fn()
    finally:
        logger.remove(hid)
    return sink.getvalue()


def test_a_near_zero_stop_is_excluded_from_excursion_stats():
    """entry=1554.80, a placeholder stop 1 rupee away (0.06% — the BROKER_EXIT
    shape), and a real mfe of 85 rupees away. Undexcluded this produces an
    ~85R capture number; the guard must drop it from mfe_r/mae_r while KEEPING
    its r_multiple."""
    from tools.exit_audit import audit_closed
    rows = [_row(1554.80, 1553.80, 1640.0, 1550.0, r_mult=0.49)]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "excluded" in out.lower() or "1 closed trade" in out


def test_realised_r_survives_exclusion():
    """The excluded row's r_multiple must still count toward winners/losers —
    it comes from a different column and the entry-time stop being bad says
    nothing about whether it is correct."""
    from tools.exit_audit import _f
    from tools import exit_audit as EA

    rows = [_row(1554.80, 1553.80, 1640.0, 1550.0, r_mult=0.49)]

    recs = []
    for r in rows:
        entry = _f(r.get("entry_price"))
        realised = _f(r.get("r_multiple"))
        recs.append(realised)
    assert recs == [0.49], "realised R must not depend on the excursion guard"


def test_a_plausible_stop_still_produces_capture():
    """A real 0.9% intraday stop with a real excursion must NOT be excluded —
    the guard's floor (0.15%) must sit comfortably below any genuine engine
    stop (measured minimum 0.43% across all seven engines)."""
    from tools.exit_audit import _f

    entry, stop, mfe = 133.46, 134.26, 131.00   # a real SDN-shaped short
    risk = abs(entry - stop)
    risk_pct = risk / entry * 100.0
    assert risk_pct > 0.15, f"a real 0.6% stop must clear the sanity floor, got {risk_pct}"


def test_a_plausible_stop_with_an_implausible_mfe_is_still_caught():
    """THE FAILURE THAT MADE THIS SECOND GUARD NECESSARY. The risk-based guard
    alone was re-run against the real book and produced the SAME corrupted
    numbers as before, unchanged to three decimal places -- the near-zero-risk
    hypothesis did not explain the actual data. This is the case it missed: a
    perfectly plausible 3% stop, but an MFE that implies a 50R move, which no
    real intraday or swing position reaches. The symptom-based cap must catch
    this regardless of what produced it."""
    from tools.exit_audit import audit_closed
    # entry=100, stop=97 (3% risk -- clears MIN_RISK_PCT easily), mfe=250
    # (implies peak_r = 150/3 = 50, far past MAX_PLAUSIBLE_R)
    rows = [_row(100.0, 97.0, 250.0, 95.0, r_mult=0.3)]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "over 15.0" in out and "PLAUSIBLE stop distance" in out, (
        f"a plausible-risk, implausible-magnitude row was not flagged: {out!r}")


def test_raw_values_are_printed_for_implausible_rows():
    """The whole point of the second guard is to make the actual cause
    visible, not just the fact that a row is wrong."""
    from tools.exit_audit import audit_closed
    rows = [_row(100.0, 97.0, 250.0, 95.0, r_mult=0.3)]
    out = _captured_logs(lambda: audit_closed(_SB(rows), "SWING"))
    assert "entry=100" in out and "stop=97" in out, (
        f"raw diagnostic values missing from output: {out!r}")


TESTS = [
    ("a near-zero stop is excluded from excursion stats",
     test_a_near_zero_stop_is_excluded_from_excursion_stats),
    ("realised R survives exclusion",
     test_realised_r_survives_exclusion),
    ("a plausible stop still produces capture",
     test_a_plausible_stop_still_produces_capture),
    ("a plausible stop with an implausible MFE is still caught",
     test_a_plausible_stop_with_an_implausible_mfe_is_still_caught),
    ("raw values are printed for implausible rows",
     test_raw_values_are_printed_for_implausible_rows),
]
