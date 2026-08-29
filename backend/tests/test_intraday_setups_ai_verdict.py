"""
PREFER/NEUTRAL/AVOID now persist per setup (30-Aug-2026, migration 128).

WHAT THIS CATCHES
-----------------
Before this, only AVOID was ever recoverable (as cost_verdict='VETOED_AI').
PREFER/NEUTRAL only nudged confidence in memory and, at best, survived in
one ai_context row per DAY (upserted, overwritten every slow tick) --
reconstructing from that gave 76 verdicts across 23 days, too thin to
measure anything. `_record_setup` now takes the real ai_advisor.Advice and
writes `ai_verdict`/`ai_source` on every row reached AFTER apply() ran.
"""

from __future__ import annotations

from intraday.ai_advisor import Advice


class _RecordingSB:
    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "intraday_setups"
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def execute(self):
        return self


class _FakeSetup:
    symbol = "TCS"
    strategy = "ORB"
    direction = "LONG"
    entry = 100.0
    stop = 99.0
    target = 102.0
    confidence = 0.6
    rationale = ""
    invalidation = ""
    risk_pct = 1.0
    reward_pct = 2.0
    rr = 2.0
    meta: dict = {}


def _engine(sb=None):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb = sb or _RecordingSB()
    eng._recorded = {}
    return eng


def test_advice_writes_verdict_and_source():
    sb = _RecordingSB()
    eng = _engine(sb)
    advice = Advice("TCS", "ORB", "PREFER", 0.10, "strong RS", "ai")
    eng._record_setup(_FakeSetup(), "PRIME", 0.0, "TAKEN", 10, advice=advice)
    row = sb.inserted[0]
    assert row["ai_verdict"] == "PREFER"
    assert row["ai_source"] == "ai"


def test_no_advice_writes_null_not_a_missing_key():
    """Absent must be a recorded None, not an omitted key -- the same rule
    ATR/regime_at_detection instrumentation already follows."""
    sb = _RecordingSB()
    eng = _engine(sb)
    eng._record_setup(_FakeSetup(), "PRIME", 0.0, "BLOCKED_STRUCTURE", 0)
    row = sb.inserted[0]
    assert "ai_verdict" in row and row["ai_verdict"] is None
    assert "ai_source" in row and row["ai_source"] is None


def test_prior_only_advice_is_distinguishable_from_a_real_ai_call():
    """source='prior' must survive -- collapsing it into 'ai' would dilute
    the NEUTRAL bucket with rows the LLM never actually reviewed."""
    sb = _RecordingSB()
    eng = _engine(sb)
    advice = Advice("TCS", "ORB", "NEUTRAL", 0.05, "ORB has resolved 40 at 55% hit", "prior")
    eng._record_setup(_FakeSetup(), "PRIME", 0.0, "TAKEN", 10, advice=advice)
    assert sb.inserted[0]["ai_source"] == "prior"


def test_every_post_ai_review_call_site_passes_advice():
    """
    Asserted against the source, not by eye -- the same discipline
    test_sdn_confidence_cap.py's test_every_sdn_setup_path_consults_it uses.
    Five call sites sit after ai_advisor.apply() in evaluate_intraday_setups
    (VETOED_AI, BELOW_CONVICTION, BLOCKED_LIQUIDITY, BLOCKED_DEPTH,
    TAKEN/REJECTED_COST) and must all pass advice=advice, so a sixth site
    added later without it fails this rather than silently staying NULL.
    """
    import inspect
    from intraday import engine as M
    src = inspect.getsource(M.IntradayEngine.evaluate_intraday_setups)
    assert src.count("advice=advice") == 5, (
        f"expected exactly 5 call sites passing advice=advice, "
        f"found {src.count('advice=advice')}")


TESTS = [
    ("advice writes ai_verdict and ai_source", test_advice_writes_verdict_and_source),
    ("no advice writes NULL, not a missing key",
     test_no_advice_writes_null_not_a_missing_key),
    ("prior-only advice keeps ai_source='prior', not collapsed into 'ai'",
     test_prior_only_advice_is_distinguishable_from_a_real_ai_call),
    ("every post-AI-review call site passes advice=advice",
     test_every_post_ai_review_call_site_passes_advice),
]
