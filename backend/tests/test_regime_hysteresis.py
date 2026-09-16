"""
compute_regime.apply_hysteresis() — the two states it could not leave.

Stored history (market_regime.regime_score_computed) exposed both:
  - 31-Aug..10-Sep-2026 scored 24-34, below the module's own RISK OFF line
    of 40, and stayed NEUTRAL: from NEUTRAL only a score under 25 twice (or
    under 15 once) could reach RISK OFF. Nifty fell ~5% across those sessions.
  - 11..24-Jun-2026 read RISK OFF at scores 45-51: RISK OFF had no exit except
    RECOVERING, which requires a score between 25 and 40.
"""

from __future__ import annotations


def _step(labels_scores: list[tuple[str, float]], today: float, row: dict | None = None) -> str:
    """labels_scores oldest -> newest; history is passed newest-first like production."""
    from swing.compute.compute_regime import apply_hysteresis
    history = [{"computed_regime": lab, "regime_score_computed": sc}
               for lab, sc in reversed(labels_scores)]
    return apply_hysteresis(today, row or {}, history, False)


def _walk(start_label: str, start_score: float, scores: list[float]) -> list[str]:
    seq = [(start_label, start_score)]
    out = []
    for s in scores:
        lab = _step(seq, s)
        out.append(lab)
        seq.append((lab, s))
    return out


def test_two_sessions_below_40_leave_neutral():
    assert _step([("NEUTRAL", 45), ("NEUTRAL", 31)], 28) == "RISK OFF"


def test_one_session_below_40_does_not_flip_neutral():
    assert _step([("NEUTRAL", 45), ("NEUTRAL", 44)], 33) == "NEUTRAL"


def test_real_sep_2026_sequence_reaches_risk_off():
    # 28-Aug 40, then 31-Aug 31, 01-Sep 28, 02-Sep 24, 04-Sep 32, 07-Sep 30,
    # 08-Sep 34, 09-Sep 25, 10-Sep 24
    labels = _walk("NEUTRAL", 40, [31, 28, 24, 32, 30, 34, 25, 24])
    assert labels[0] == "NEUTRAL", labels
    assert all(l == "RISK OFF" for l in labels[1:]), labels


def test_risk_off_exits_after_three_sessions_at_or_above_40():
    assert _step([("RISK OFF", 20), ("RISK OFF", 46), ("RISK OFF", 50)], 48) == "NEUTRAL"


def test_risk_off_upgrade_stays_slower_than_the_downgrade():
    assert _step([("RISK OFF", 20), ("RISK OFF", 46)], 50) == "RISK OFF"


def test_real_jun_2026_sequence_leaves_risk_off():
    # 11-Jun 20 (RISK OFF), then 46, 50, 48, 50, 51, 45, 49
    labels = _walk("RISK OFF", 20, [46, 50, 48, 50, 51, 45, 49])
    assert labels[:2] == ["RISK OFF", "RISK OFF"], labels
    assert all(l == "NEUTRAL" for l in labels[2:]), labels


def test_ordinary_neutral_tape_stays_neutral():
    labels = _walk("NEUTRAL", 50, [48, 52, 44, 41, 55, 49])
    assert set(labels) == {"NEUTRAL"}, labels


def test_existing_fast_downgrade_still_fires():
    assert _step([("NEUTRAL", 45)], 12) == "RISK OFF"


TESTS = [
    ("two sessions below 40 leave NEUTRAL", test_two_sessions_below_40_leave_neutral),
    ("one session below 40 does not flip NEUTRAL", test_one_session_below_40_does_not_flip_neutral),
    ("real 31-Aug..10-Sep-2026 scores reach RISK OFF", test_real_sep_2026_sequence_reaches_risk_off),
    ("RISK OFF exits after three sessions >= 40", test_risk_off_exits_after_three_sessions_at_or_above_40),
    ("RISK OFF upgrade stays slower than the downgrade", test_risk_off_upgrade_stays_slower_than_the_downgrade),
    ("real 11..24-Jun-2026 scores leave RISK OFF", test_real_jun_2026_sequence_leaves_risk_off),
    ("ordinary NEUTRAL tape stays NEUTRAL", test_ordinary_neutral_tape_stays_neutral),
    ("existing <15 fast downgrade still fires", test_existing_fast_downgrade_still_fires),
]


class _RegimeTable:
    """market_regime fake that honours the filters load_history() may apply."""
    def __init__(self, rows):
        self.rows, self.f = rows, []
    def select(self, *_):
        return self
    def order(self, *_a, **_k):
        return self
    def limit(self, n):
        self.n = n
        return self
    def lt(self, col, v):
        self.f.append(lambda r: r[col] < v)
        return self
    def lte(self, col, v):
        self.f.append(lambda r: r[col] <= v)
        return self
    def execute(self):
        rows = [r for r in self.rows if all(f(r) for f in self.f)]
        rows = sorted(rows, key=lambda r: r["date"], reverse=True)[:self.n]
        return type("R", (), {"data": rows})()


def test_history_is_point_in_time_on_a_rerun():
    """25-Jun-2026 was recomputed on 28-Jun after the ML classifier had created an
    unlabelled 29-Jun row; that row became 'yesterday' and NEUTRAL by default."""
    from swing.compute.compute_regime import load_history, apply_hysteresis
    rows = [
        {"date": "2026-06-23", "computed_regime": "RISK OFF", "regime": "RISK OFF", "regime_score_computed": 29.0},
        {"date": "2026-06-24", "computed_regime": "RISK OFF", "regime": "RISK OFF", "regime_score_computed": 35.0},
        {"date": "2026-06-25", "computed_regime": "RISK OFF", "regime": "RISK OFF", "regime_score_computed": 36.0},
        {"date": "2026-06-29", "computed_regime": None, "regime": None, "regime_score_computed": None},
    ]
    sb = type("SB", (), {"table": lambda self, name: _RegimeTable(rows)})()
    hist = load_history(sb, "2026-06-25")
    assert [h["date"] for h in hist] == ["2026-06-24", "2026-06-23"], [h["date"] for h in hist]
    assert apply_hysteresis(36.0, {}, hist, False) == "RISK OFF"


def test_history_skips_unlabelled_rows():
    from swing.compute.compute_regime import load_history
    rows = [
        {"date": "2026-09-09", "computed_regime": "RISK OFF", "regime": "RISK OFF", "regime_score_computed": 25.0},
        {"date": "2026-09-10", "computed_regime": None, "regime": None, "regime_score_computed": None},
    ]
    sb = type("SB", (), {"table": lambda self, name: _RegimeTable(rows)})()
    hist = load_history(sb, "2026-09-11")
    assert [h["date"] for h in hist] == ["2026-09-09"], [h["date"] for h in hist]


TESTS += [
    ("history is point-in-time on a re-run (25-Jun-2026)", test_history_is_point_in_time_on_a_rerun),
    ("history skips unlabelled rows", test_history_skips_unlabelled_rows),
]
