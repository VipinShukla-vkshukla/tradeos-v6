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
