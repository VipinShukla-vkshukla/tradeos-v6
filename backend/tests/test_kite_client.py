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
