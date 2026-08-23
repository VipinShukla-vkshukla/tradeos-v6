"""
kite/kite_client.py::_is_mainboard_symbol() — the pure predicate behind
fetch_nse_eq_symbols(). Stage D2e, 23-Aug-2026 (docs/TRADEOS_ROADMAP.md,
Track D). Extracted specifically because it has already been wrong twice
this session in ways only live data caught: `instrument_type == "EQ"`
filtered nothing (every row on this endpoint carries that tag), and the
`-XX` suffix filter alone left 343 `INAV` reference-price symbols in —
none of which have EVER appeared in raw_prices, checked against its full
history. Neither gap could have been caught without a live Kite session
until this predicate existed on its own to test directly.
"""

from __future__ import annotations


def _with_name_cache(names: dict[str, str]):
    """Populates kite_client._instr_cache["names"] the way fetch_nse_eq_
    symbols() would, without a live Kite session — is_etf_name() reads
    only that cache, never calls Kite itself."""
    import kite.kite_client as kc
    kc._instr_cache["names"] = names


def test_is_etf_name_true_for_a_known_etf():
    """Stage D2g, 24-Aug-2026 — the operator's own question: does the
    Kite same-day diff let a new ETF through as if it were a stock IPO?
    Checked live: Kite's instrument_type is "EQ" for NIFTYBEES exactly
    as it is for RELIANCE — name is the only field that tells them apart."""
    from kite.kite_client import is_etf_name
    _with_name_cache({"NIFTYBEES": "NIPPON INDIA ETF NIFTY 50 BEES"})
    assert is_etf_name("NIFTYBEES") is True


def test_is_etf_name_false_for_a_real_stock():
    from kite.kite_client import is_etf_name
    _with_name_cache({"RELIANCE": "RELIANCE INDUSTRIES", "MILKYMIST": "MILKY MIST DAIRY FOOD L"})
    assert is_etf_name("RELIANCE") is False
    assert is_etf_name("MILKYMIST") is False


def test_is_etf_name_false_for_a_symbol_not_in_the_cache():
    """Advisory only — a symbol the cache has never seen must not be
    treated as an ETF by default (that would silently drop it from
    being reported as new, the opposite of this project's cold-start
    rule: absence must not be read as a negative)."""
    from kite.kite_client import is_etf_name
    _with_name_cache({})
    assert is_etf_name("UNKNOWN") is False


def test_is_etf_name_case_insensitive():
    from kite.kite_client import is_etf_name
    _with_name_cache({"SOMEFUND": "some lowercase etf name"})
    assert is_etf_name("SOMEFUND") is True


def test_is_mainboard_symbol_accepts_a_plain_equity():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("RELIANCE") is True


def test_is_mainboard_symbol_accepts_a_plain_etf():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("NIFTYBEES") is True


def test_is_mainboard_symbol_rejects_bond_sdl_suffix():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("SOMEBOND-N3") is False


def test_is_mainboard_symbol_rejects_sme_board_suffix():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("GOLDSTAR-SM") is False


def test_is_mainboard_symbol_rejects_trade_to_trade_suffix():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("SOMESTOCK-BE") is False


def test_is_mainboard_symbol_rejects_inav_reference_feed():
    """The actual gap found live 23-Aug: 343 INAV symbols passed the
    original suffix-only filter and none of them have ever traded."""
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("GOLDBEINAV") is False


def test_is_mainboard_symbol_rejects_empty_string():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("") is False


def test_is_mainboard_symbol_rejects_none():
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol(None) is False


def test_is_mainboard_symbol_does_not_reject_a_name_that_merely_contains_inav():
    """The check is a SUFFIX match, not a substring match — a real
    mainboard name that happens to contain the letters must not be
    rejected on that basis alone. (No known real symbol does this; this
    documents the boundary the implementation actually draws.)"""
    from kite.kite_client import _is_mainboard_symbol
    assert _is_mainboard_symbol("INAVCORP") is True


TESTS = [
    ("is_etf_name true for a known ETF", test_is_etf_name_true_for_a_known_etf),
    ("is_etf_name false for a real stock", test_is_etf_name_false_for_a_real_stock),
    ("is_etf_name false for a symbol not in the cache", test_is_etf_name_false_for_a_symbol_not_in_the_cache),
    ("is_etf_name case insensitive", test_is_etf_name_case_insensitive),
    ("is_mainboard_symbol accepts a plain equity", test_is_mainboard_symbol_accepts_a_plain_equity),
    ("is_mainboard_symbol accepts a plain etf", test_is_mainboard_symbol_accepts_a_plain_etf),
    ("is_mainboard_symbol rejects bond/SDL suffix", test_is_mainboard_symbol_rejects_bond_sdl_suffix),
    ("is_mainboard_symbol rejects SME board suffix", test_is_mainboard_symbol_rejects_sme_board_suffix),
    ("is_mainboard_symbol rejects trade-to-trade suffix", test_is_mainboard_symbol_rejects_trade_to_trade_suffix),
    ("is_mainboard_symbol rejects INAV reference feed", test_is_mainboard_symbol_rejects_inav_reference_feed),
    ("is_mainboard_symbol rejects empty string", test_is_mainboard_symbol_rejects_empty_string),
    ("is_mainboard_symbol rejects None", test_is_mainboard_symbol_rejects_none),
    ("is_mainboard_symbol does not reject a name that merely contains INAV", test_is_mainboard_symbol_does_not_reject_a_name_that_merely_contains_inav),
]
