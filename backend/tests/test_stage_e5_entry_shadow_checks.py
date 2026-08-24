"""
Track E, Stage E5 pieces 2-3 (docs/TRADEOS_ROADMAP.md), shipped SHADOW
ONLY per F-75's own conclusion: the evidence (n=16 closed positions, zero
CAUTION-bucket rows) is too thin to set a confident hard-refusal
threshold without risking a bar no real winner can clear. Both switches
default off; `analysis.entry_ranking.entry_refusals()` logs what each
would have caught so the next quantify pass has real accumulated data.

Piece 2 — R:R retention: F-74's own anchor. HAL's real numbers at its
actual entry-day zone snapshot: rr_at_zone_low=7.63, rr_live=1.17 ->
retention 0.153. entry_rr_retention_floor defaults to 0.20 specifically
so this real anchor case lights up the shadow rather than sitting just
above a tighter floor (0.153 > 0.15 would have silently missed its own
motivating example).

Piece 3 — broken trend at entry: reuses control.exit_rules.assess_trend()
(this session's own F-75 fix) on the CANDIDATE itself, not just an
already-held position — the same BROKEN bar the exit-side rules already
trust to cut a losing position.
"""

from __future__ import annotations

from tests import cfg_ctx


def _broken_sig() -> dict:
    """A signal context assess_trend() reads as BROKEN — mirrors the
    fixture already established in test_stage_e4_early_invalidation_and_
    sector_decay.py for the exact same purpose."""
    return {
        "symbol": "X",
        "dist_sma50": -8.0, "rsi_daily": 28.0, "adx": 12.0,
        "vol_ratio": 0.4, "rs_vs_nifty": -3.0, "sector_rank_at_entry": 18,
    }


# ── Piece 2: R:R retention ──────────────────────────────────────────────────

def test_rr_retention_shadow_only_by_default():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({}):
        out = entry_refusals({"symbol": "HAL"}, rr_live=1.17, rr_at_zone_low=7.63)
    assert not out, (
        "entry_refuse_low_rr_retention is off by default — a collapsed "
        f"R:R must not refuse yet, got {out}")


def test_rr_retention_refuses_when_armed_using_hals_own_numbers():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_refuse_low_rr_retention": "true"}):
        out = entry_refusals({"symbol": "HAL"}, rr_live=1.17, rr_at_zone_low=7.63)
    assert out, (
        "HAL's own real retention (1.17/7.63=0.153) is below the 0.20 "
        "default floor — must refuse when armed")
    assert "retention" in out[0].lower() or "R:R" in out[0]


def test_rr_retention_does_not_fire_on_healthy_retention():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_refuse_low_rr_retention": "true"}):
        out = entry_refusals({"symbol": "X"}, rr_live=3.0, rr_at_zone_low=10.0)
    assert not out, (
        f"retention 0.30 is above the 0.20 floor — must not refuse, got {out}")


def test_rr_retention_no_op_without_both_values():
    """A plan can legitimately have no live figure (e.g. decide() never
    ran) — must not fabricate a refusal from missing data."""
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_refuse_low_rr_retention": "true"}):
        assert not entry_refusals({"symbol": "X"}, rr_live=1.17, rr_at_zone_low=None)
        assert not entry_refusals({"symbol": "X"}, rr_live=None, rr_at_zone_low=7.63)


# ── Piece 3: broken trend at entry ──────────────────────────────────────────

def test_broken_trend_shadow_only_by_default():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({}):
        out = entry_refusals(_broken_sig())
    assert not out, (
        f"entry_refuse_broken_trend is off by default — must not refuse "
        f"yet, got {out}")


def test_broken_trend_refuses_when_armed_and_structure_is_broken():
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_refuse_broken_trend": "true"}):
        out = entry_refusals(_broken_sig())
    assert out, "genuinely BROKEN trend evidence must refuse when armed"
    assert any("BROKEN" in r for r in out)


def test_broken_trend_does_not_fire_without_broken_evidence():
    """An ordinary, unremarkable candidate must not be refused just
    because the switch is armed — has_evidence + BROKEN is still
    required, same bar the exit-side rules already trust."""
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_refuse_broken_trend": "true"}):
        out = entry_refusals({"symbol": "X"})   # no trend data at all
    assert not out, f"no evidence must not manufacture a refusal, got {out}"


# ── Existing behaviour, unchanged ───────────────────────────────────────────

def test_existing_refusal_checks_unaffected_by_new_signature():
    """entry_refusals()'s new rr_live/rr_at_zone_low kwargs default to
    None — every pre-existing call site (which passes neither) must
    behave exactly as before."""
    from analysis.entry_ranking import entry_refusals
    with cfg_ctx({"entry_rank_respect_ai_avoid": "true"}):
        out = entry_refusals({"symbol": "GABRIEL", "eap_action": "AVOID_ENTRY",
                              "ai_risks": "extended, mean reversion likely"})
    assert out and "AVOID_ENTRY" in out[0]


TESTS = [
    ("R:R retention shadow-only by default",
     test_rr_retention_shadow_only_by_default),
    ("R:R retention refuses when armed, using HAL's own real numbers",
     test_rr_retention_refuses_when_armed_using_hals_own_numbers),
    ("R:R retention does not fire on healthy retention",
     test_rr_retention_does_not_fire_on_healthy_retention),
    ("R:R retention is a no-op without both values",
     test_rr_retention_no_op_without_both_values),
    ("broken trend at entry shadow-only by default",
     test_broken_trend_shadow_only_by_default),
    ("broken trend at entry refuses when armed and structure is broken",
     test_broken_trend_refuses_when_armed_and_structure_is_broken),
    ("broken trend at entry does not fire without broken evidence",
     test_broken_trend_does_not_fire_without_broken_evidence),
    ("existing refusal checks unaffected by the new signature",
     test_existing_refusal_checks_unaffected_by_new_signature),
]
