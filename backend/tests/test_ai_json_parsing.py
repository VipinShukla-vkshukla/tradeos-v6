"""
Two AI JSON-response failures from the 2026-08-25 evening pipeline run.

WHAT THIS CATCHES
------------------
Step 19 (ai_decision_engine): batch 3/6 of the 25-Aug run came back as
well-formed JSON — json.loads succeeded — but one entry in
ranked_candidates was a bare string instead of a {symbol, tier, ...}
object. That is a SHAPE failure, not a syntax one, so nothing in the parse
layer caught it: call_ai_batched's merge loop hit
`row.get("symbol")` on that string and crashed with
"'str' object has no attribute 'get'", taking out the whole step even
though its own docstring says one bad batch must not do that.

Step 18 (market_intelligence_engine): the same evening's response failed
with "Expecting ',' delimiter: line 209 column 54" — a raw newline inside
a string value, not truncation. The only repair path available
(_close_truncated_json) only closes brackets for a response cut off at the
end, so it returned None and step 18 exited with nothing, discarding the
whole market overlay for the day. ai_decision_engine.py already carries a
proven fix for this exact failure class (strict=False, then escape control
chars inside strings); step 18 now reuses it before falling back to
truncation handling.

Two evenings later (2026-08-27), step 18 failed again with a DIFFERENT
error shape from the same class of problem: "Expecting property name
enclosed in double quotes: line 208 column 5" — a trailing comma before a
`}` mid-document. None of the existing repair tiers touch this (strict=False
and the control-char escaper don't apply, and _close_truncated_json only
helps when the break is at the true end of the response). step 18 now falls
back to the json_repair library — the same soft dependency
ai_decision_engine.py already uses as its own last resort — after
_close_truncated_json has had its chance, so the genuinely-truncated path
is unaffected.
"""

from __future__ import annotations

import json

from ai.ai_decision_engine import _parse_ai_json, _sanitize_ranked_candidates
from ai.market_intelligence_engine import _parse_ai_json as _intel_parse_ai_json
from ai.market_intelligence_engine import _close_truncated_json


def test_sanitize_drops_non_dict_ranked_candidates():
    """The exact shape of the 25-Aug batch-3 crash: a bare string sitting
    where an object was expected. Must be dropped, not raise."""
    result = {
        "ranked_candidates": [
            {"symbol": "DATAPATTNS", "tier": "TIER_2", "confidence": 0.7},
            "IDEA",
            {"symbol": "IFCI", "tier": "TIER_3", "confidence": 0.4},
        ]
    }
    cleaned = _sanitize_ranked_candidates(result)
    assert [r["symbol"] for r in cleaned["ranked_candidates"]] == ["DATAPATTNS", "IFCI"]


def test_sanitize_is_a_no_op_on_well_formed_input():
    """Every entry already an object — nothing should be dropped or altered."""
    rows = [{"symbol": "A", "tier": "TIER_1"}, {"symbol": "B", "tier": "TIER_2"}]
    result = {"ranked_candidates": list(rows)}
    cleaned = _sanitize_ranked_candidates(result)
    assert cleaned["ranked_candidates"] == rows


def test_sanitize_leaves_missing_or_non_list_key_alone():
    """No ranked_candidates key at all, or a non-list value, must pass
    through unchanged rather than raise."""
    assert _sanitize_ranked_candidates({}) == {}
    result = {"ranked_candidates": None}
    assert _sanitize_ranked_candidates(result) == {"ranked_candidates": None}


def test_decision_engine_parse_json_drops_bad_rows_end_to_end():
    """The full parse path (call_ai_batched's entry point): a raw AI text
    blob with a bad row must come back with that row gone, not raise."""
    full_text = json.dumps({
        "ranked_candidates": [
            {"symbol": "AAA", "tier": "TIER_1", "confidence": 0.9},
            "BBB",
        ],
        "summary": "ok",
    })
    result = _parse_ai_json(full_text)
    assert result is not None
    assert [r["symbol"] for r in result["ranked_candidates"]] == ["AAA"]


def test_market_intel_parse_recovers_raw_newline_in_string_value():
    """Reproduces the 25-Aug step 18 failure: valid JSON structure, but a
    literal newline sits inside a string value instead of an escaped \\n.
    json.loads(strict=True) rejects this with an 'Expecting , delimiter'
    error at the newline; the fix must recover it instead of returning
    None and discarding the whole market overlay."""
    bad_but_recoverable = (
        '{"market_tone": {"position_sizing_guidance": "normal"}, '
        '"macro_sector_impacts": [], "regulatory_alerts": [], '
        '"fii_outlook": "neutral", "dii_outlook": "neutral", '
        '"candidate_sentiment": [{"symbol": "X", '
        '"note": "line one\nline two"}]}'
    )
    result = _intel_parse_ai_json(bad_but_recoverable, n_candidates=1)
    assert result is not None
    assert result["candidate_sentiment"][0]["symbol"] == "X"


def test_market_intel_parse_still_recovers_genuinely_truncated_response():
    """A response cut off mid-object (no control-char issue at all) must
    still fall through to _close_truncated_json exactly as before —
    the new repair tiers must not shadow the existing truncation path.
    All required keys are closed via a bracket before the truncation
    point (a second, half-written candidate_sentiment entry) so recovery
    doesn't depend on the separate scalar-tail limitation covered by
    test_close_truncated_json_handles_deeper_nesting_after_last_close."""
    truncated = (
        '{"fii_outlook": "neutral", "dii_outlook": "neutral", '
        '"market_tone": {"position_sizing_guidance": "normal"}, '
        '"macro_sector_impacts": [], "regulatory_alerts": [], '
        '"candidate_sentiment": [{"symbol": "X"}, {"symbol": "Y"'
    )
    result = _intel_parse_ai_json(truncated, n_candidates=1)
    assert result is not None
    assert result["candidate_sentiment"] == [{"symbol": "X"}]


def test_market_intel_parse_recovers_trailing_comma_mid_document():
    """Reproduces the 27-Aug step 18 failure: "Expecting property name
    enclosed in double quotes: line 208 column 5" — a trailing comma before
    a `}` sitting mid-document, not at the end. The response closes
    properly overall, so _close_truncated_json's trim-and-reclose just
    reproduces the same broken document and returns None; only the
    json_repair last resort can fix a broken key/value pair like this."""
    bad_trailing_comma = (
        '{"market_tone": {"position_sizing_guidance": "normal"}, '
        '"macro_sector_impacts": [], "regulatory_alerts": [], '
        '"fii_outlook": "neutral", "dii_outlook": "neutral", '
        '"candidate_sentiment": [{"symbol": "X", "note": "ok",}]}'
    )
    result = _intel_parse_ai_json(bad_trailing_comma, n_candidates=1)
    assert result is not None
    assert result["candidate_sentiment"][0]["symbol"] == "X"


def test_close_truncated_json_closes_the_textbook_mid_array_case():
    """The most basic truncation shape there is: cut off partway through
    the second of two sibling arrays. Found broken while writing this test
    suite — the closer was built from the stack at the END of the raw
    text (which still had the dropped, half-written "b" array open on it)
    instead of the stack at the trim point (only the outer object open),
    so it appended one closing bracket too many and always failed."""
    truncated = '{"a": [1,2,3], "b": [4,5,'
    fixed = _close_truncated_json(truncated)
    assert fixed is not None
    assert json.loads(fixed) == {"a": [1, 2, 3]}


def test_close_truncated_json_handles_deeper_nesting_after_last_close():
    """Same bug, at a nested checkpoint: the last COMPLETE value sits two
    levels deep (inside an array inside the outer object), and a second,
    incomplete entry starts after it. The closer must match the stack as
    it stood right after that last complete entry (array + outer object
    still open = 2 closers), not the deeper stack reached while scanning
    the dropped, half-written second entry (3 closers, one too many)."""
    truncated = (
        '{"market_tone": {"position_sizing_guidance": "normal"}, '
        '"macro_sector_impacts": [], "regulatory_alerts": [], '
        '"candidate_sentiment": [{"symbol": "X"}, {"symbol": "Y"'
    )
    fixed = _close_truncated_json(truncated)
    assert fixed is not None
    parsed = json.loads(fixed)
    assert parsed["candidate_sentiment"] == [{"symbol": "X"}]


TESTS = [
    ("sanitize drops non-dict ranked_candidates entries",
     test_sanitize_drops_non_dict_ranked_candidates),
    ("sanitize is a no-op on well-formed input",
     test_sanitize_is_a_no_op_on_well_formed_input),
    ("sanitize leaves missing/non-list key alone",
     test_sanitize_leaves_missing_or_non_list_key_alone),
    ("decision engine parse_json drops bad rows end-to-end",
     test_decision_engine_parse_json_drops_bad_rows_end_to_end),
    ("market intel parse recovers raw newline in string value",
     test_market_intel_parse_recovers_raw_newline_in_string_value),
    ("market intel parse still recovers genuinely truncated response",
     test_market_intel_parse_still_recovers_genuinely_truncated_response),
    ("market intel parse recovers trailing comma mid-document",
     test_market_intel_parse_recovers_trailing_comma_mid_document),
    ("close_truncated_json closes the textbook mid-array case",
     test_close_truncated_json_closes_the_textbook_mid_array_case),
    ("close_truncated_json handles deeper nesting after last close",
     test_close_truncated_json_handles_deeper_nesting_after_last_close),
]
