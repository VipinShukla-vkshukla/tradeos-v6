"""
Stage D2f — NSE IPO archive refresh (docs/TRADEOS_ROADMAP.md, Track D).

Covers `build_rows()`, the pure record-parsing logic `swing/ingestion/
ingest_ipo_listings.py::main()` delegates to. The network fetch and
Supabase write are not exercised here — same boundary `test_ingest_asm_
gsm.py`/`test_ingest_nifty_total_market.py` already draw for their own
modules.
"""

from __future__ import annotations


def _nse_row(symbol="ABC", company="ABC Ltd", security_type="EQ",
            issue_price="140", price_range="Rs.133 to Rs.140",
            start="11-AUG-2026", end="13-AUG-2026", listing="18-AUG-2026"):
    return {"symbol": symbol, "company": company, "securityType": security_type,
           "issuePrice": issue_price, "priceRange": price_range,
           "ipoStartDate": start, "ipoEndDate": end, "listingDate": listing}


def test_build_rows_parses_a_normal_record():
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row()], "2026-08-24T00:00:00Z")
    r = out[0]
    assert r["symbol"] == "ABC"
    assert r["security_type"] == "EQ"
    assert r["issue_price"] == 140.0
    assert r["price_range_low"] == 133.0 and r["price_range_high"] == 140.0
    assert r["issue_start_date"] == "2026-08-11"
    assert r["issue_end_date"] == "2026-08-13"
    assert r["listing_date"] == "2026-08-18"


def test_build_rows_treats_dash_listing_date_as_none():
    """Not yet listed — the exact case Sunshine Pictures/Shankesh
    Jewellers showed live, 24-Aug-2026: issue closed, listing pending."""
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row(listing="-")], "t")
    assert out[0]["listing_date"] is None


def test_build_rows_treats_dash_issue_price_as_none():
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row(issue_price="-")], "t")
    assert out[0]["issue_price"] is None


def test_build_rows_skips_a_record_with_no_symbol():
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row(symbol="")], "t")
    assert out == []


def test_build_rows_uppercases_symbol():
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row(symbol="milkymist")], "t")
    assert out[0]["symbol"] == "MILKYMIST"


def test_build_rows_dedupes_repeated_symbol_keeping_first_occurrence():
    """The real bug found live 24-Aug: NSE's archive carries one row per
    bond/NCD TRANCHE, not per company — IBULHSG appeared 13 times. A raw
    upsert of the unmodified feed raises Postgres 21000 ("cannot affect
    row a second time") before writing anything at all."""
    from swing.ingestion.ingest_ipo_listings import build_rows
    rows = [
        _nse_row(symbol="IBULHSG", listing="09-APR-2024"),
        _nse_row(symbol="IBULHSG", listing="09-APR-2024"),
        _nse_row(symbol="IBULHSG", listing="09-APR-2024"),
    ]
    out = build_rows(rows, "t")
    assert len(out) == 1
    assert out[0]["symbol"] == "IBULHSG"


def test_build_rows_parses_a_price_range_with_commas():
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row(price_range="Rs.1,200 to Rs.1,250")], "t")
    assert out[0]["price_range_low"] == 1200.0
    assert out[0]["price_range_high"] == 1250.0


def test_build_rows_handles_malformed_price_range_gracefully():
    from swing.ingestion.ingest_ipo_listings import build_rows
    out = build_rows([_nse_row(price_range="garbage")], "t")
    assert out[0]["price_range_low"] is None
    assert out[0]["price_range_high"] is None


TESTS = [
    ("build_rows parses a normal record", test_build_rows_parses_a_normal_record),
    ("build_rows treats dash listing_date as None", test_build_rows_treats_dash_listing_date_as_none),
    ("build_rows treats dash issue_price as None", test_build_rows_treats_dash_issue_price_as_none),
    ("build_rows skips a record with no symbol", test_build_rows_skips_a_record_with_no_symbol),
    ("build_rows uppercases symbol", test_build_rows_uppercases_symbol),
    ("build_rows dedupes repeated symbol keeping first occurrence", test_build_rows_dedupes_repeated_symbol_keeping_first_occurrence),
    ("build_rows parses a price range with commas", test_build_rows_parses_a_price_range_with_commas),
    ("build_rows handles malformed price range gracefully", test_build_rows_handles_malformed_price_range_gracefully),
]
