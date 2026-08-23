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

from tests import cfg_ctx


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
    contents in the same fake client."""
    def __init__(self, by_table: dict[str, list[dict]]):
        self._by_table = by_table

    def table(self, name):
        rows = self._by_table.get(name, [])

        class _Query:
            def __init__(self, rows): self._rows = rows
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def in_(self, *a, **k): return self
            def execute(self): return self
            @property
            def data(self): return self._rows

        return _Query(rows)


def test_unreferenced_candidates_finds_nifty_total_market_gap():
    """Population B: a name in nifty_total_market but absent from
    stock_data_daily must come back with atr_pct=0.0 and a reason naming
    the source honestly — there is no history to be relative to."""
    from intraday.scanner import unreferenced_candidates
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
    """Population C: a name in neither table — the genuine new-listing
    case — surfaces only through Kite's own instrument master."""
    from intraday.scanner import unreferenced_candidates
    sb = _TableRouter({
        "stock_data_daily": [{"symbol": "TRACKED"}],
        "nifty_total_market": [{"symbol": "TRACKED", "company_name": "x", "industry": "bank"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols",
              return_value={"TRACKED", "FRESHLISTED"}):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["FRESHLISTED"]
    assert "Kite instrument master" in out[0].reason


def test_unreferenced_candidates_never_duplicates_a_name_seen_in_both_sources():
    from intraday.scanner import unreferenced_candidates
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [{"symbol": "BOTH", "company_name": "x", "industry": "it"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", return_value={"BOTH"}):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["BOTH"], "must appear once, from whichever source is checked first"


def test_unreferenced_candidates_respects_exclude():
    from intraday.scanner import unreferenced_candidates
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
    sb = _TableRouter({
        "stock_data_daily": [],
        "nifty_total_market": [{"symbol": "STILLFOUND", "company_name": "x", "industry": "it"}],
    })
    with patch("intraday.scanner._latest_date", return_value="2026-08-23"), \
         patch("kite.kite_client.fetch_nse_eq_symbols", side_effect=Exception("no token")):
        out = unreferenced_candidates(sb)
    assert [e.symbol for e in out] == ["STILLFOUND"]


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
    ("live_requalify min_price rejects a cheap name even if move/turnover clear", test_live_requalify_min_price_rejects_a_cheap_name_even_if_move_and_turnover_clear),
    ("live_requalify min_price=None means no price gate at all", test_live_requalify_min_price_none_means_no_price_gate_at_all),
]
