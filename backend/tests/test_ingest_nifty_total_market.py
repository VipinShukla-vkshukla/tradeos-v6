"""
Stage D2b — nifty_total_market weekly refresh
(docs/TRADEOS_ROADMAP.md, Track D).

Covers `build_payload()`, the pure row-construction logic
`swing/ingestion/ingest_nifty_total_market.py::main()` delegates to. The
network fetch and Supabase write are not exercised here — same boundary
`test_ingest_asm_gsm.py` already draws for its own module.
"""

from __future__ import annotations


def _tm_row(symbol="ABC", company="ABC Ltd.", industry="IT", isin="INE000A00000", series="EQ"):
    return {"Symbol": symbol, "Company Name": company, "Industry": industry,
           "ISIN Code": isin, "Series": series}


def test_build_payload_sets_both_flags_when_both_fetches_ok():
    from swing.ingestion.ingest_nifty_total_market import build_payload
    rows = [_tm_row("IN200"), _tm_row("IN500ONLY"), _tm_row("INNEITHER")]
    out = build_payload(rows, {"IN200"}, {"IN200", "IN500ONLY"},
                        n200_ok=True, n500_ok=True, refreshed_at="2026-08-23T00:00:00Z")
    by_sym = {r["symbol"]: r for r in out}
    assert by_sym["IN200"]["nifty_200"] is True and by_sym["IN200"]["nifty_500"] is True
    assert by_sym["IN500ONLY"]["nifty_200"] is False and by_sym["IN500ONLY"]["nifty_500"] is True
    assert by_sym["INNEITHER"]["nifty_200"] is False and by_sym["INNEITHER"]["nifty_500"] is False


def test_build_payload_omits_nifty_200_key_entirely_when_that_fetch_failed():
    """The exact mechanism that keeps a failed index fetch from silently
    overwriting an existing flag: the key must be ABSENT from the row
    dict, not present-and-False -- an upsert only touches keys it's given."""
    from swing.ingestion.ingest_nifty_total_market import build_payload
    out = build_payload([_tm_row("X")], set(), {"X"},
                        n200_ok=False, n500_ok=True, refreshed_at="t")
    assert "nifty_200" not in out[0]
    assert out[0]["nifty_500"] is True


def test_build_payload_omits_nifty_500_key_entirely_when_that_fetch_failed():
    from swing.ingestion.ingest_nifty_total_market import build_payload
    out = build_payload([_tm_row("X")], {"X"}, set(),
                        n200_ok=True, n500_ok=False, refreshed_at="t")
    assert out[0]["nifty_200"] is True
    assert "nifty_500" not in out[0]


def test_build_payload_omits_both_when_both_fetches_failed():
    from swing.ingestion.ingest_nifty_total_market import build_payload
    out = build_payload([_tm_row("X")], set(), set(),
                        n200_ok=False, n500_ok=False, refreshed_at="t")
    assert "nifty_200" not in out[0] and "nifty_500" not in out[0]
    # Base fields still refresh even when neither index flag can.
    assert out[0]["symbol"] == "X" and out[0]["refreshed_at"] == "t"


def test_build_payload_skips_a_row_with_no_symbol():
    from swing.ingestion.ingest_nifty_total_market import build_payload
    rows = [_tm_row("GOOD"), _tm_row("")]
    out = build_payload(rows, set(), set(), n200_ok=True, n500_ok=True, refreshed_at="t")
    assert [r["symbol"] for r in out] == ["GOOD"]


def test_build_payload_uppercases_and_strips_symbol():
    from swing.ingestion.ingest_nifty_total_market import build_payload
    out = build_payload([_tm_row("  abc  ")], set(), set(),
                        n200_ok=True, n500_ok=True, refreshed_at="t")
    assert out[0]["symbol"] == "ABC"


def test_build_payload_blank_optional_field_becomes_none_not_empty_string():
    from swing.ingestion.ingest_nifty_total_market import build_payload
    out = build_payload([_tm_row("X", industry="")], set(), set(),
                        n200_ok=True, n500_ok=True, refreshed_at="t")
    assert out[0]["industry"] is None


TESTS = [
    ("build_payload sets both flags when both fetches ok", test_build_payload_sets_both_flags_when_both_fetches_ok),
    ("build_payload omits nifty_200 key entirely when that fetch failed", test_build_payload_omits_nifty_200_key_entirely_when_that_fetch_failed),
    ("build_payload omits nifty_500 key entirely when that fetch failed", test_build_payload_omits_nifty_500_key_entirely_when_that_fetch_failed),
    ("build_payload omits both when both fetches failed", test_build_payload_omits_both_when_both_fetches_failed),
    ("build_payload skips a row with no symbol", test_build_payload_skips_a_row_with_no_symbol),
    ("build_payload uppercases and strips symbol", test_build_payload_uppercases_and_strips_symbol),
    ("build_payload blank optional field becomes None not empty string", test_build_payload_blank_optional_field_becomes_none_not_empty_string),
]
