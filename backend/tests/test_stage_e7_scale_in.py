"""
Track E, Stage E7 (docs/TRADEOS_ROADMAP.md) — position scaling, the only
stage in this whole track that adds capital risk rather than sharpening a
decision already being made. Detection/sizing only this session, per the
operator's own confirmed scope ("quantify first, ship OFF by default,
shadow-log before arming") — `evaluate_scale_in()`'s own docstring
explains why execution is not built: how a combined position's risk is
measured post-add (blended entry_price, or the add's own economics
governing the R-multiple going forward) is a real, unresolved accounting
question, and shipping execution before it is answered risks corrupting
the R-multiple/giveback math this whole track has spent five stages
getting right.

Quantify pass, 24-Aug-2026: of 17 recent closed SWING trades with usable
MFE data, only 2 (both PPLPHARMA) ever crossed the 1.0R runner line at
their peak — scale-in opportunities are rare on this book, matching how
rarely trades reach the 3R hard target the RUN decision already governs.
"""

from __future__ import annotations

from tests import cfg_ctx


class _TQ:
    """Minimal stand-in for control.exit_rules.TrendQuality — only the
    two fields evaluate_scale_in() actually reads."""
    def __init__(self, verdict, has_evidence=True, checks=6):
        self.verdict = verdict
        self.has_evidence = has_evidence
        self.checks = checks


def _pos(entry=100.0, stop=94.0, active=None, scaled_in=False, **kw) -> dict:
    p = {"symbol": "X", "entry_price": entry, "planned_stop": stop,
         "active_sl": active if active is not None else stop,
         "current_qty": 10, "actual_qty": 10, "invested_value": 1000.0,
         "sector": "test sector", "industry": "test industry",
         "scaled_in": scaled_in}
    p.update(kw)
    return p


def test_below_runner_line_no_add():
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=108.0)   # breakeven-plus, matching F-43's tiering
    ltp = 100.0 + 0.5 * 6.0    # +0.5R — below the 1.0R line
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, ltp, _TQ("STRONG"), [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "below_runner_line"


def test_already_scaled_in_no_add():
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=108.0, scaled_in=True)
    ltp = 100.0 + 2.5 * 6.0   # well past the line
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, ltp, _TQ("STRONG"), [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "already_scaled"


def test_intact_is_not_strong_enough():
    """A stricter bar than target_decision()'s own should_run (STRONG-
    or-INTACT) — INTACT is enough to let an existing runner continue,
    not enough to commit NEW risk."""
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=108.0)
    ltp = 100.0 + 2.5 * 6.0
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, ltp, _TQ("INTACT"), [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "not_strong_enough"


def test_strong_without_evidence_no_add():
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=108.0)
    ltp = 100.0 + 2.5 * 6.0
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, ltp, _TQ("STRONG", has_evidence=False),
                              [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "not_strong_enough"


def test_no_room_below_current_stop():
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=120.0)   # stop AT/ABOVE the live price below
    ltp = 100.0 + 2.5 * 6.0    # 115 — below active_sl=120
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, ltp, _TQ("STRONG"), [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "no_room_below_stop"


def test_qualifying_case_scales_in_with_real_sizing():
    """Every rail cleared: past the runner line, STRONG with evidence,
    never scaled, real room below the current stop. Sized through the
    REAL check_new_entry() (not mocked) — generous capital and a lone,
    modest position so every cap clears comfortably and the qty is
    deterministic."""
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=108.0)
    ltp = 100.0 + 2.5 * 6.0   # 115.0, +2.5R
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, ltp, _TQ("STRONG"), [pos], total_capital=100000)
    assert d["action"] == "SCALE_IN", d
    assert d["add_qty"] > 0
    # add_risk_per_share must be priced off the CURRENT stop (115-108=7),
    # never off the position's own unrealized profit (115-100=15, the
    # classic pyramiding-on-paper-gains mistake this function exists to
    # avoid).
    assert d["add_risk_per_share"] == 7.0


def test_check_new_entry_refusal_is_respected():
    """A book already at its position-count cap must refuse the add —
    an add competes for the SAME slot/risk budget any fresh entry would,
    not a special-cased exemption."""
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(active=108.0)
    other_positions = [_pos(symbol=f"Y{i}", sector="test sector",
                            industry="test industry") for i in range(7)]
    ltp = 100.0 + 2.5 * 6.0
    with cfg_ctx({"max_positions_neutral": "7"}):
        d = evaluate_scale_in(pos, ltp, _TQ("STRONG"),
                              other_positions + [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "max_positions"


def test_missing_baseline_no_add():
    from control.position_lifecycle import evaluate_scale_in
    pos = _pos(entry=0.0, stop=0.0)
    with cfg_ctx({}):
        d = evaluate_scale_in(pos, 100.0, _TQ("STRONG"), [pos], total_capital=100000)
    assert d["action"] == "NO_ADD"
    assert d["reason"] == "no_baseline"


TESTS = [
    ("below runner line -> NO_ADD", test_below_runner_line_no_add),
    ("already scaled in -> NO_ADD", test_already_scaled_in_no_add),
    ("INTACT is not STRONG enough", test_intact_is_not_strong_enough),
    ("STRONG without evidence -> NO_ADD", test_strong_without_evidence_no_add),
    ("no room below the current stop -> NO_ADD",
     test_no_room_below_current_stop),
    ("qualifying case scales in with real sizing",
     test_qualifying_case_scales_in_with_real_sizing),
    ("check_new_entry refusal is respected",
     test_check_new_entry_refusal_is_respected),
    ("missing baseline -> NO_ADD", test_missing_baseline_no_add),
]
