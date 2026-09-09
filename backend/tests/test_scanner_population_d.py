"""
Population D — scanner.unranked_qualifying_candidates() (docs/FINDINGS.md,
09/10-Sep-2026). Names that pass every static gate but were outranked out
of today's bench. Companion to test_scanner_live_requalify.py's Population
A/B coverage.
"""

from __future__ import annotations

from unittest.mock import patch

from tests import cfg_ctx


class _Query:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return self
    @property
    def data(self): return self._rows


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _Query(self._rows)


def _row(**over):
    base = {"symbol": "TEST", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
            "delivery_pct": 30.0, "avg_vol_20d": 1_000_000.0, "sector": "auto",
            "asm_flag": False, "fo_ban_flag": False}
    base.update(over)
    return base


def test_unranked_qualifying_candidates_admits_a_clean_outranked_row():
    from intraday.scanner import unranked_qualifying_candidates
    rows = [_row(symbol="OUTRANKED")]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-09-09"):
            out = unranked_qualifying_candidates(_SB(rows))
    assert [e.symbol for e in out] == ["OUTRANKED"]
    assert out[0].source == "population_d"


def test_unranked_qualifying_candidates_rejects_a_genuine_gate_failure():
    """A real _qualifies() rejection (ASM-flagged here) stays rejected —
    Population D only rescues names that already pass every gate."""
    from intraday.scanner import unranked_qualifying_candidates
    rows = [_row(symbol="FLAGGED", asm_flag=True)]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-09-09"):
            out = unranked_qualifying_candidates(_SB(rows))
    assert out == []


def test_unranked_qualifying_candidates_respects_exclude():
    from intraday.scanner import unranked_qualifying_candidates
    rows = [_row(symbol="ALREADY_BENCHED")]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-09-09"):
            out = unranked_qualifying_candidates(_SB(rows), exclude={"ALREADY_BENCHED"})
    assert out == [], "a name already on the bench (or another population) must not be re-admitted"


def test_unranked_qualifying_candidates_is_unbounded_not_top_n():
    """No rank-based cap — every qualifying, unexcluded row comes back,
    matching movement_rejected_candidates()'s own deliberately unbounded
    shape (see the function's docstring)."""
    from intraday.scanner import unranked_qualifying_candidates
    rows = [_row(symbol=f"NAME{i}") for i in range(25)]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-09-09"):
            out = unranked_qualifying_candidates(_SB(rows))
    assert len(out) == 25


def test_unranked_qualifying_candidates_catches_the_09sep_missed_movers():
    """Regression fixture for the operator's own 09-Sep-2026 review:
    GRAPHITE/JSL/SARDAEN/PTCIL/PPLPHARMA all qualified outright and were
    invisible all session because none ranked into the bench. Shaped as
    ordinary qualifying rows (Population D has no knowledge of the day's
    actual move — it only sees yesterday's stock_data_daily, same as
    every other population here) to prove the mechanism would have
    surfaced them for a live re-check."""
    from intraday.scanner import unranked_qualifying_candidates
    missed = ["GRAPHITE", "JSL", "SARDAEN", "PTCIL", "PPLPHARMA"]
    rows = [_row(symbol=sym, close=250.0, value_cr=80.0, atr_pct=2.5,
                 delivery_pct=35.0) for sym in missed]
    # A crowded bench of higher-ranked names sits alongside them, exactly
    # as it would live -- Population D doesn't care about rank, only gates.
    rows += [_row(symbol=f"BENCHED{i}", value_cr=500.0) for i in range(40)]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-09-09"):
            out = unranked_qualifying_candidates(
                _SB(rows), exclude={f"BENCHED{i}" for i in range(40)})
    assert {e.symbol for e in out} == set(missed), (
        f"expected exactly the missed movers, got {[e.symbol for e in out]}")


TESTS = [
    ("unranked_qualifying_candidates admits a clean outranked row",
     test_unranked_qualifying_candidates_admits_a_clean_outranked_row),
    ("unranked_qualifying_candidates rejects a genuine gate failure",
     test_unranked_qualifying_candidates_rejects_a_genuine_gate_failure),
    ("unranked_qualifying_candidates respects exclude",
     test_unranked_qualifying_candidates_respects_exclude),
    ("unranked_qualifying_candidates is unbounded, not top-N",
     test_unranked_qualifying_candidates_is_unbounded_not_top_n),
    ("unranked_qualifying_candidates catches the 09-Sep missed movers",
     test_unranked_qualifying_candidates_catches_the_09sep_missed_movers),
]
