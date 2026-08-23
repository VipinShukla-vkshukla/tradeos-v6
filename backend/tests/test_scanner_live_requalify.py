"""
Stage D2 — live universe re-qualification (docs/TRADEOS_ROADMAP.md, Track D).

WHAT THIS COVERS
-----------------
`_qualifies()` — the per-row gate extracted from build_universe() so that
function and movement_rejected_candidates() can never independently drift
on what "otherwise tradeable" means (the exact class of bug the hurdle/edge
units mismatch and the sub_engine overwrite both were, one level up).

`movement_rejected_candidates()` — the population a live re-check is
allowed to touch: qualifies on price/liquidity/delivery, failed ONLY on
yesterday's ATR band.

`live_requalify()` — pure admission logic against today's own live quote
data. This is the one that actually decides whether a name gets watched,
so it gets the most thorough coverage here.

`build_universe()` is NOT re-tested for its pre-existing behaviour here —
that would duplicate tools/replay/universe.py's own drift guard. What IS
checked is that the refactor to route through `_qualifies()` did not change
its rejected-count/kept-list shape, via one direct fixture.
"""

from __future__ import annotations

from unittest.mock import patch

import intraday.scanner as scanner_mod
from tests import cfg_ctx


def _reset_scanner_caches():
    """_daily_reference_reads()/new_listings() cache once per real calendar
    day at module level — correct in production, but it means two tests in
    the SAME `tools.verify` process would otherwise share whatever the
    first one populated, since both run on the same real date. Every test
    touching unreferenced_candidates()/new_listings() must call this first."""
    scanner_mod._ref_cache.update(date=None, known=None, market_rows=None, flagged=None)
    scanner_mod._baseline_cache.update(date=None, symbols=None)


# ── _qualifies ────────────────────────────────────────────────────────────

def _row(**over):
    base = {"symbol": "TEST", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
           "delivery_pct": 30.0, "asm_flag": False, "fo_ban_flag": False}
    base.update(over)
    return base


_GATES = dict(min_price=50.0, min_value=25.0, min_atr=1.20, max_atr=8.00, min_deliv=20.0,
             skip_flagged=True)


def test_qualifies_passes_a_clean_row():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(), **_GATES)
    assert ok and reason is None


def test_qualifies_rejects_below_min_price():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(close=40.0), **_GATES)
    assert not ok and reason == "price"


def test_qualifies_rejects_flagged_when_skip_flagged_true():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(asm_flag=True), **_GATES)
    assert not ok and reason == "flagged"


def test_qualifies_admits_flagged_when_skip_flagged_false():
    from intraday.scanner import _qualifies
    gates = {**_GATES, "skip_flagged": False}
    ok, reason = _qualifies(_row(asm_flag=True), **gates)
    assert ok and reason is None


def test_qualifies_rejects_below_min_turnover():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(value_cr=10.0), **_GATES)
    assert not ok and reason == "liquidity"


def test_qualifies_rejects_atr_below_floor():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(atr_pct=0.5), **_GATES)
    assert not ok and reason == "movement"


def test_qualifies_rejects_atr_above_ceiling():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(atr_pct=9.0), **_GATES)
    assert not ok and reason == "movement"


def test_qualifies_rejects_below_min_delivery():
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(delivery_pct=5.0), **_GATES)
    assert not ok and reason == "delivery"


def test_qualifies_require_movement_false_skips_the_atr_check_entirely():
    """The exact lever movement_rejected_candidates() needs: a row whose
    ATR alone would fail must still pass every OTHER gate when movement
    is not being checked."""
    from intraday.scanner import _qualifies
    gates = {**_GATES, "require_movement": False}
    ok, reason = _qualifies(_row(atr_pct=0.1), **gates)
    assert ok and reason is None


def test_qualifies_checks_gates_in_price_flagged_liquidity_movement_delivery_order():
    """A row failing multiple gates reports the FIRST one, matching
    build_universe()'s own rejected-counter shape exactly (one row,
    one bucket) — order matters for that counter to stay meaningful."""
    from intraday.scanner import _qualifies
    ok, reason = _qualifies(_row(close=10.0, value_cr=1.0), **_GATES)
    assert not ok and reason == "price"


# ── build_universe() refactor did not change behaviour ──────────────────

def test_build_universe_refactor_preserves_rejection_shape():
    """Same fixture shape as tools/replay/universe.py's own drift guard,
    exercised directly against scanner.build_universe() this time — proves
    the _qualifies() extraction changed nothing observable."""
    from intraday.scanner import build_universe

    class _Query:
        def __init__(self, rows): self._rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return self
        @property
        def data(self): return self._rows

    class _SB:
        def __init__(self, rows): self._rows = rows
        def table(self, name): return _Query(self._rows)

    rows = [
        {"symbol": "GOOD", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "bank",
         "asm_flag": False, "fo_ban_flag": False},
        {"symbol": "CHEAP", "close": 10.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "auto",
         "asm_flag": False, "fo_ban_flag": False},
        {"symbol": "FLAGGED", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "it",
         "asm_flag": True, "fo_ban_flag": False},
        {"symbol": "QUIET", "close": 100.0, "value_cr": 50.0, "atr_pct": 0.5,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "energy",
         "asm_flag": False, "fo_ban_flag": False},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-08-23"):
            kept = build_universe(_SB(rows), limit=40)
    assert [e.symbol for e in kept] == ["GOOD"], (
        f"expected only GOOD to survive, got {[e.symbol for e in kept]}")


# ── movement_rejected_candidates() ───────────────────────────────────────

def test_movement_rejected_candidates_returns_only_the_atr_only_failures():
    from intraday.scanner import movement_rejected_candidates

    class _Query:
        def __init__(self, rows): self._rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return self
        @property
        def data(self): return self._rows

    class _SB:
        def __init__(self, rows): self._rows = rows
        def table(self, name): return _Query(self._rows)

    rows = [
        # Fails ONLY on ATR -- exactly the population wanted.
        {"symbol": "QUIET", "close": 100.0, "value_cr": 50.0, "atr_pct": 0.5,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "energy",
         "asm_flag": False, "fo_ban_flag": False},
        # Fails on price TOO -- not our population even though ATR is also low.
        {"symbol": "CHEAP_AND_QUIET", "close": 10.0, "value_cr": 50.0, "atr_pct": 0.3,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "auto",
         "asm_flag": False, "fo_ban_flag": False},
        # Already qualifies outright -- would already be in the daily bench.
        {"symbol": "ALREADY_IN", "close": 100.0, "value_cr": 50.0, "atr_pct": 2.0,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "bank",
         "asm_flag": False, "fo_ban_flag": False},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-08-23"):
            out = movement_rejected_candidates(_SB(rows))
    assert [e.symbol for e in out] == ["QUIET"], (
        f"expected only QUIET, got {[e.symbol for e in out]}")


def test_movement_rejected_candidates_respects_exclude():
    from intraday.scanner import movement_rejected_candidates

    class _Query:
        def __init__(self, rows): self._rows = rows
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return self
        @property
        def data(self): return self._rows

    class _SB:
        def __init__(self, rows): self._rows = rows
        def table(self, name): return _Query(self._rows)

    rows = [
        {"symbol": "QUIET", "close": 100.0, "value_cr": 50.0, "atr_pct": 0.5,
         "delivery_pct": 30.0, "avg_vol_20d": 1000.0, "sector": "energy",
         "asm_flag": False, "fo_ban_flag": False},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        with patch("intraday.scanner._latest_date", return_value="2026-08-23"):
            out = movement_rejected_candidates(_SB(rows), exclude={"QUIET"})
    assert out == [], "an excluded symbol (already in the bench) must never be re-checked"


# ── live_requalify() ─────────────────────────────────────────────────────

def _candidate(symbol="QUIET", atr_pct=0.5):
    from intraday.scanner import UniverseEntry
    return UniverseEntry(symbol=symbol, close=100.0, value_cr=50.0, atr_pct=atr_pct,
                         delivery_pct=30.0, sector="energy", score=0.0,
                         reason="test fixture", avg_vol_20d=1000.0)


def _quote(ltp=110.0, close=100.0, volume=3_000_000, avg_price=105.0):
    return {"ltp": ltp, "close": close, "volume": volume, "avg_price": avg_price}


def test_live_requalify_admits_a_candidate_clearing_both_floors():
    from intraday.scanner import live_requalify
    # move = (110-100)/100 = 10%; turnover = 3e6*105/1e7 = 31.5cr
    out = live_requalify([_candidate()], {"QUIET": _quote()}, move_pct=1.20, turnover_cr=25.0)
    assert len(out) == 1 and out[0].symbol == "QUIET"
    assert "LIVE REQUALIFIED" in out[0].reason


def test_live_requalify_refuses_move_ok_but_turnover_too_thin():
    from intraday.scanner import live_requalify
    thin = _quote(volume=100_000, avg_price=105.0)  # ~1.05cr, well under 25cr
    out = live_requalify([_candidate()], {"QUIET": thin}, move_pct=1.20, turnover_cr=25.0)
    assert out == []


def test_live_requalify_refuses_turnover_ok_but_move_too_small():
    from intraday.scanner import live_requalify
    barely_moved = _quote(ltp=100.3, close=100.0)  # 0.3% move, under 1.20% floor
    out = live_requalify([_candidate()], {"QUIET": barely_moved}, move_pct=1.20, turnover_cr=25.0)
    assert out == []


def test_live_requalify_admits_a_downward_move_same_as_upward():
    """The floor is a MAGNITUDE test, matching build_universe()'s own ATR
    band being direction-agnostic — a stock down 10% is exactly as
    tradeable-for-movement as one up 10%."""
    from intraday.scanner import live_requalify
    down = _quote(ltp=90.0, close=100.0)  # -10%
    out = live_requalify([_candidate()], {"QUIET": down}, move_pct=1.20, turnover_cr=25.0)
    assert len(out) == 1


def test_live_requalify_skips_a_candidate_with_no_quote():
    """Absence is not evidence either way — same rule live_rerank() already
    applies to a symbol with no live tick yet."""
    from intraday.scanner import live_requalify
    out = live_requalify([_candidate()], {}, move_pct=1.20, turnover_cr=25.0)
    assert out == []


def test_live_requalify_skips_zero_prev_close_defensively():
    from intraday.scanner import live_requalify
    bad = _quote(close=0.0)
    out = live_requalify([_candidate()], {"QUIET": bad}, move_pct=1.20, turnover_cr=25.0)
    assert out == [], "a zero previous close must never divide-by-zero into a false admit"


def test_live_requalify_carries_yesterdays_real_atr_through_honestly():
    """The admitted entry's atr_pct is the REAL (sub-floor) historical
    value, not a fabricated one — so a downstream reader can always see
    why this name was not in the ordinary daily bench."""
    from intraday.scanner import live_requalify
    out = live_requalify([_candidate(atr_pct=0.42)], {"QUIET": _quote()},
                         move_pct=1.20, turnover_cr=25.0)
    assert out[0].atr_pct == 0.42


# ── unreferenced_candidates() ────────────────────────────────────────────

class _TableRouter:
    """Routes .table(name) to whichever fixture rows that name should return
    — stock_data_daily, nifty_total_market and safety_lists need independent
    contents in the same fake client. .rpc(name, params) routes similarly,
    keyed by rpc name, for get_raw_prices_first_seen()."""
    def __init__(self, by_table: dict[str, list[dict]], by_rpc: dict[str, list[dict]] | None = None):
        self._by_table = by_table
        self._by_rpc = by_rpc or {}

    def table(self, name):
        rows = self._by_table.get(name, [])

        class _Query:
            def __init__(self, rows): self._rows = rows
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def gt(self, *a, **k): return self
            def in_(self, *a, **k): return self
            def order(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def range(self, *a, **k): return self   # fixtures stay well under fetch_all()'s 1000-row page
            def execute(self): return self
            @property
            def data(self): return self._rows

        return _Query(rows)

    def rpc(self, name, params=None):
        rows = self._by_rpc.get(name, [])

        class _RpcCall:
            def __init__(self, rows): self._rows = rows
            def execute(self): return self
            @property
            def data(self): return self._rows

        return _RpcCall(rows)


def test_unreferenced_candidates_finds_nifty_total_market_gap():
    """Population B: a name in nifty_total_market but absent from
    stock_data_daily must come back with atr_pct=0.0 and a reason naming
    the source honestly — there is no history to be relative to."""
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [{"symbol": "TRACKED"}],
        "nifty_total_market": [
            {"symbol": "TRACKED", "company_name": "x", "industry": "bank"},
            {"symbol": "UNTRACKED", "company_name": "y", "industry": "auto"},
        ],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", return_value=set()):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["UNTRACKED"]
    assert out[0].atr_pct == 0.0
    assert "nifty_total_market" in out[0].reason


def test_unreferenced_candidates_finds_kite_only_names_beyond_both_tables():
    """Population C: a name in neither table AND never seen in a prior
    kite_symbol_baseline diff — the genuine new-listing case. The baseline
    fixture carries an unrelated already-known symbol so this exercises
    the STEADY-STATE diff, not the bootstrap (see the dedicated bootstrap
    test below for that path)."""
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [{"symbol": "TRACKED"}],
        "nifty_total_market": [{"symbol": "TRACKED", "company_name": "x", "industry": "bank"}],
        "kite_symbol_baseline": [{"symbol": "SOME_OLD_SYMBOL"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols",
              return_value={"TRACKED", "FRESHLISTED"}):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["FRESHLISTED"]
    assert "never seen in Kite's instrument master" in out[0].reason


def test_unreferenced_candidates_never_duplicates_a_name_seen_in_both_sources():
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [{"symbol": "BOTH", "company_name": "x", "industry": "it"}],
        "kite_symbol_baseline": [{"symbol": "SOME_OLD_SYMBOL"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", return_value={"BOTH"}):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["BOTH"], "must appear once, from whichever source is checked first"


def test_unreferenced_candidates_respects_exclude():
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [{"symbol": "ALREADY_BENCHED", "company_name": "x", "industry": "it"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", return_value=set()):
        out = unreferenced_candidates(sb, exclude={"ALREADY_BENCHED"})
    assert out == []


def test_unreferenced_candidates_excludes_a_name_on_safety_lists():
    """The gap live_requalify()'s own min_price check can't close: an ASM/
    GSM/FO_BAN name has no asm_flag/fo_ban_flag column to be read from at
    all for this population, so safety_lists is the only place that can
    answer it."""
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [
            {"symbol": "CLEAN", "company_name": "x", "industry": "bank"},
            {"symbol": "BANNED", "company_name": "y", "industry": "metal"},
        ],
        "safety_lists": [{"symbol": "BANNED"}],
    })
    with cfg_ctx({"intraday_skip_flagged": "true"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", return_value=set()):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["CLEAN"]


def test_unreferenced_candidates_admits_flagged_when_skip_flagged_false():
    """Same override _qualifies() itself honours -- skip_flagged=false must
    let a flagged name through here too, not silently keep blocking it."""
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [{"symbol": "BANNED", "company_name": "y", "industry": "metal"}],
        "safety_lists": [{"symbol": "BANNED"}],
    })
    with cfg_ctx({"intraday_skip_flagged": "false"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", return_value=set()):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["BANNED"]


def test_unreferenced_candidates_survives_kite_master_unavailable():
    """Same 'advisory only' contract as everything else touching Kite —
    an empty/failed instrument fetch must degrade to Population B only,
    never raise."""
    from intraday.scanner import unreferenced_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [{"symbol": "STILLFOUND", "company_name": "x", "industry": "it"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", side_effect=Exception("no token")):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["STILLFOUND"]


# ── new_listings() — the Kite-baseline diff itself ───────────────────────

def _days_ago(n: int) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


def test_new_listings_bootstrap_seeds_everything_and_reports_nothing_new():
    """Stage D2f, back to the simple F-57 shape: raw_prices-based recency
    classification during bootstrap was scrapped by the operator (real
    coverage-gap false positives found live -- see recent_ipo_candidates()
    for the replacement, sourced from NSE's own confirmed IPO archive
    instead). On the very first run ever, an empty baseline seeds the
    whole current universe as already-known and reports NOTHING -- there
    is no listing history to diff against, so 'new' stays undefined."""
    from intraday.scanner import new_listings
    _reset_scanner_caches()
    sb = _TableRouter({"kite_symbol_baseline": []})
    with patch("kite.kite_client.fetch_nse_eq_symbols",
              return_value={"A", "B", "C"}):
        out = new_listings(sb)
    assert out == set()


# ── recent_ipo_candidates() — NSE's own confirmed IPO archive ───────────

def test_recent_ipo_candidates_returns_a_confirmed_recent_listing():
    from intraday.scanner import recent_ipo_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "ipo_listings": [
            {"symbol": "MILKYMIST", "company_name": "Milky Mist Dairy Food Ltd",
             "listing_date": _days_ago(6)},
        ],
    })
    with cfg_ctx({"intraday_ipo_recency_days": "45"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"):
        out = recent_ipo_candidates(sb)
    assert [e.symbol for e in out] == ["MILKYMIST"]
    assert "NSE-confirmed IPO" in out[0].reason
    assert out[0].atr_pct == 0.0


def test_recent_ipo_candidates_respects_exclude():
    from intraday.scanner import recent_ipo_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "ipo_listings": [
            {"symbol": "MILKYMIST", "company_name": "x", "listing_date": _days_ago(6)}],
    })
    with cfg_ctx({"intraday_ipo_recency_days": "45"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"):
        out = recent_ipo_candidates(sb, exclude={"MILKYMIST"})
    assert out == []


def test_recent_ipo_candidates_excludes_a_symbol_already_in_stock_data_daily():
    """A name already tracked has no business in Population C -- it's
    already part of the main daily bench build_universe() produces."""
    from intraday.scanner import recent_ipo_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "stock_data_daily": [{"symbol": "MILKYMIST"}],
        "ipo_listings": [
            {"symbol": "MILKYMIST", "company_name": "x", "listing_date": _days_ago(6)}],
    })
    with cfg_ctx({"intraday_ipo_recency_days": "45"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"):
        out = recent_ipo_candidates(sb)
    assert out == []


def test_recent_ipo_candidates_excludes_a_flagged_symbol():
    from intraday.scanner import recent_ipo_candidates
    _reset_scanner_caches()
    sb = _TableRouter({
        "ipo_listings": [
            {"symbol": "BANNED", "company_name": "x", "listing_date": _days_ago(6)}],
        "safety_lists": [{"symbol": "BANNED"}],
    })
    with cfg_ctx({"intraday_ipo_recency_days": "45", "intraday_skip_flagged": "true"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"):
        out = recent_ipo_candidates(sb)
    assert out == []


def test_recent_ipo_candidates_survives_a_read_failure():
    from intraday.scanner import recent_ipo_candidates

    class _RaisingSB(_TableRouter):
        def table(self, name):
            if name == "ipo_listings":
                raise Exception("table not found")
            return super().table(name)

    _reset_scanner_caches()
    sb = _RaisingSB({})
    with cfg_ctx({"intraday_ipo_recency_days": "45"}), \
         patch("intraday.scanner._latest_date", return_value="2026-08-23"):
        out = recent_ipo_candidates(sb)
    assert out == []


def test_new_listings_steady_state_reports_only_truly_new_symbols():
    from intraday.scanner import new_listings
    _reset_scanner_caches()
    sb = _TableRouter({"kite_symbol_baseline": [{"symbol": "OLD1"}, {"symbol": "OLD2"}]})
    with patch("kite.kite_client.fetch_nse_eq_symbols",
              return_value={"OLD1", "OLD2", "TRULY_NEW"}):
        out = new_listings(sb)
    assert out == {"TRULY_NEW"}


def test_new_listings_previously_seen_symbol_never_reported_twice():
    """The other half of the mechanism: once a symbol has been recorded
    (by an earlier run, or earlier in THIS run via the in-process cache),
    it must not keep showing up as 'new' on every subsequent check."""
    from intraday.scanner import new_listings
    _reset_scanner_caches()
    sb = _TableRouter({"kite_symbol_baseline": [{"symbol": "OLD1"}]})
    with patch("kite.kite_client.fetch_nse_eq_symbols",
              return_value={"OLD1", "FRESH"}):
        first = new_listings(sb)
        second = new_listings(sb)   # same process, same cached day
    assert first == {"FRESH"}
    assert second == set(), "FRESH was already reported once this run — must not repeat"


def test_new_listings_empty_live_set_returns_empty_without_crashing():
    from intraday.scanner import new_listings
    _reset_scanner_caches()
    sb = _TableRouter({"kite_symbol_baseline": [{"symbol": "OLD1"}]})
    with patch("kite.kite_client.fetch_nse_eq_symbols", return_value=set()):
        out = new_listings(sb)
    assert out == set()


def test_new_listings_survives_kite_fetch_raising():
    """Same 'advisory only, never take the system down' contract as every
    other function that touches Kite in this module — new_listings() must
    degrade to empty on its own, not rely on unreferenced_candidates()'
    try/except around it as the only backstop."""
    from intraday.scanner import new_listings
    _reset_scanner_caches()
    sb = _TableRouter({"kite_symbol_baseline": [{"symbol": "OLD1"}]})
    with patch("kite.kite_client.fetch_nse_eq_symbols",
              side_effect=Exception("no token")):
        out = new_listings(sb)
    assert out == set()


def test_live_requalify_min_price_rejects_a_cheap_name_even_if_move_and_turnover_clear():
    """The gate unreferenced_candidates() specifically needs: a penny-priced
    name can clear move% and turnover-cr on volume alone (both are scale-
    free of price), so without this it would sail through with none of
    _qualifies()'s own price floor ever having been asked."""
    from intraday.scanner import live_requalify
    penny = _candidate(symbol="PENNY")
    q = _quote(ltp=8.5, close=7.5, volume=40_000_000, avg_price=8.0)  # ~13% move, ~32cr
    out = live_requalify([penny], {"PENNY": q}, move_pct=1.20, turnover_cr=25.0, min_price=50.0)
    assert out == []


def test_live_requalify_min_price_none_means_no_price_gate_at_all():
    """movement_rejected_candidates() callers that don't pass min_price
    keep today's exact prior behaviour -- this is an additive gate, not a
    silent tightening of Population A's existing path."""
    from intraday.scanner import live_requalify
    penny = _candidate(symbol="PENNY")
    q = _quote(ltp=8.5, close=7.5, volume=40_000_000, avg_price=8.0)
    out = live_requalify([penny], {"PENNY": q}, move_pct=1.20, turnover_cr=25.0)
    assert len(out) == 1


TESTS = [
    ("qualifies passes a clean row", test_qualifies_passes_a_clean_row),
    ("qualifies rejects below min price", test_qualifies_rejects_below_min_price),
    ("qualifies rejects flagged when skip_flagged true", test_qualifies_rejects_flagged_when_skip_flagged_true),
    ("qualifies admits flagged when skip_flagged false", test_qualifies_admits_flagged_when_skip_flagged_false),
    ("qualifies rejects below min turnover", test_qualifies_rejects_below_min_turnover),
    ("qualifies rejects atr below floor", test_qualifies_rejects_atr_below_floor),
    ("qualifies rejects atr above ceiling", test_qualifies_rejects_atr_above_ceiling),
    ("qualifies rejects below min delivery", test_qualifies_rejects_below_min_delivery),
    ("qualifies require_movement=False skips the atr check entirely", test_qualifies_require_movement_false_skips_the_atr_check_entirely),
    ("qualifies checks gates in a fixed order", test_qualifies_checks_gates_in_price_flagged_liquidity_movement_delivery_order),
    ("build_universe refactor preserves rejection shape", test_build_universe_refactor_preserves_rejection_shape),
    ("movement_rejected_candidates returns only the atr-only failures", test_movement_rejected_candidates_returns_only_the_atr_only_failures),
    ("movement_rejected_candidates respects exclude", test_movement_rejected_candidates_respects_exclude),
    ("live_requalify admits a candidate clearing both floors", test_live_requalify_admits_a_candidate_clearing_both_floors),
    ("live_requalify refuses move ok but turnover too thin", test_live_requalify_refuses_move_ok_but_turnover_too_thin),
    ("live_requalify refuses turnover ok but move too small", test_live_requalify_refuses_turnover_ok_but_move_too_small),
    ("live_requalify admits a downward move same as upward", test_live_requalify_admits_a_downward_move_same_as_upward),
    ("live_requalify skips a candidate with no quote", test_live_requalify_skips_a_candidate_with_no_quote),
    ("live_requalify skips zero prev close defensively", test_live_requalify_skips_zero_prev_close_defensively),
    ("live_requalify carries yesterday's real atr through honestly", test_live_requalify_carries_yesterdays_real_atr_through_honestly),
    ("unreferenced_candidates finds nifty_total_market gap", test_unreferenced_candidates_finds_nifty_total_market_gap),
    ("unreferenced_candidates finds kite-only names beyond both tables", test_unreferenced_candidates_finds_kite_only_names_beyond_both_tables),
    ("unreferenced_candidates never duplicates a name seen in both sources", test_unreferenced_candidates_never_duplicates_a_name_seen_in_both_sources),
    ("unreferenced_candidates respects exclude", test_unreferenced_candidates_respects_exclude),
    ("unreferenced_candidates excludes a name on safety_lists", test_unreferenced_candidates_excludes_a_name_on_safety_lists),
    ("unreferenced_candidates admits flagged when skip_flagged false", test_unreferenced_candidates_admits_flagged_when_skip_flagged_false),
    ("unreferenced_candidates survives kite master unavailable", test_unreferenced_candidates_survives_kite_master_unavailable),
    ("new_listings bootstrap seeds everything and reports nothing new", test_new_listings_bootstrap_seeds_everything_and_reports_nothing_new),
    ("recent_ipo_candidates returns a confirmed recent listing", test_recent_ipo_candidates_returns_a_confirmed_recent_listing),
    ("recent_ipo_candidates respects exclude", test_recent_ipo_candidates_respects_exclude),
    ("recent_ipo_candidates excludes a symbol already in stock_data_daily", test_recent_ipo_candidates_excludes_a_symbol_already_in_stock_data_daily),
    ("recent_ipo_candidates excludes a flagged symbol", test_recent_ipo_candidates_excludes_a_flagged_symbol),
    ("recent_ipo_candidates survives a read failure", test_recent_ipo_candidates_survives_a_read_failure),
    ("new_listings steady state reports only truly new symbols", test_new_listings_steady_state_reports_only_truly_new_symbols),
    ("new_listings previously seen symbol never reported twice", test_new_listings_previously_seen_symbol_never_reported_twice),
    ("new_listings empty live set returns empty without crashing", test_new_listings_empty_live_set_returns_empty_without_crashing),
    ("new_listings survives kite fetch raising", test_new_listings_survives_kite_fetch_raising),
    ("live_requalify min_price rejects a cheap name even if move/turnover clear", test_live_requalify_min_price_rejects_a_cheap_name_even_if_move_and_turnover_clear),
    ("live_requalify min_price=None means no price gate at all", test_live_requalify_min_price_none_means_no_price_gate_at_all),
]
