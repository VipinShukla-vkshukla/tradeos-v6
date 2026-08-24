"""
tools/approve_candidate.py::decide() — Stage D6, 24-Aug-2026 (docs/
TRADEOS_ROADMAP.md, Track D, branch feat/intraday-evolution).

Pure, no I/O — refuses a non-ENGINE_CANDIDATE row, refuses a row that is
not PENDING (already approved, already rejected, whatever), refuses a
row from_proposal() cannot template (never approves something that would
silently produce zero shadow activity), approves a genuinely well-formed
candidate.
"""

from __future__ import annotations


def _row(proposal_type="ENGINE_CANDIDATE", status="PENDING",
        target_key="UNSEEN/ADX > 25 (trending)", proposal_id=7,
        evidence=None, confidence=0.55):
    if evidence is None:
        evidence = {"summary": "x", "avg_move_pct": 3.0, "lift": 2.0, "n_miss": 10}
    return {"id": proposal_id, "proposal_type": proposal_type, "status": status,
            "target_key": target_key, "evidence": evidence, "confidence": confidence}


def test_approves_a_well_formed_pending_candidate():
    from tools.approve_candidate import decide
    ok, detail = decide(_row())
    assert ok is True
    assert "feature=ADX > 25" in detail


def test_refuses_non_engine_candidate():
    from tools.approve_candidate import decide
    ok, detail = decide(_row(proposal_type="FEATURE_FILTER"))
    assert ok is False
    assert "not ENGINE_CANDIDATE" in detail


def test_refuses_already_shadow_approved():
    from tools.approve_candidate import decide
    ok, detail = decide(_row(status="APPROVED"))
    assert ok is False
    assert "not PENDING" in detail


def test_refuses_already_rejected():
    from tools.approve_candidate import decide
    ok, detail = decide(_row(status="REJECTED"))
    assert ok is False
    assert "not PENDING" in detail


def test_refuses_a_row_that_cannot_be_templated():
    """Never approve something that would silently produce zero shadow
    activity -- e.g. a Pass A subject, or the already-GDB-covered feature."""
    from tools.approve_candidate import decide
    ok, detail = decide(_row(target_key="BLOCKED_STRUCTURE/PDL"))
    assert ok is False
    assert "NO shadow activity" in detail


def test_refuses_old_shape_evidence():
    from tools.approve_candidate import decide
    ok, detail = decide(_row(evidence="plain string, pre-Stage-D6"))
    assert ok is False
    assert "NO shadow activity" in detail


TESTS = [
    ("approves a well-formed PENDING candidate", test_approves_a_well_formed_pending_candidate),
    ("refuses non-ENGINE_CANDIDATE", test_refuses_non_engine_candidate),
    ("refuses already APPROVED", test_refuses_already_shadow_approved),
    ("refuses already REJECTED", test_refuses_already_rejected),
    ("refuses a row that cannot be templated", test_refuses_a_row_that_cannot_be_templated),
    ("refuses old-shape evidence", test_refuses_old_shape_evidence),
]
