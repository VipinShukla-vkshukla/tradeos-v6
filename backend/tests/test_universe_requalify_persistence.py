"""
Live-requalified admissions surviving the 300s slow-cycle rebuild (15-Sep-2026).
See intraday/engine.py::refresh_universe, refresh_contexts,
_merge_carried_bench, _merge_carried_contexts.

WHAT THIS CATCHES
------------------
`live_requalify_universe()` admits a name outside the static daily universe
by appending it to `self._bench` — its own docstring states the intent
explicitly: "a newly-admitted name starts ticking within one of THIS timer's
cycles, not the slow one's." But `refresh_universe()` (the 300s slow-cycle
rebuild) did `self._bench = scanner.universe(self.sb)`, an unconditional
full replace that discarded any such admission the instant it ran — and
`refresh_contexts()` (the same cadence) did `self._contexts = built`,
discarding any bench-only context `merge_live_bars()` had tick-built for an
admitted name in the meantime. Confirmed live: 2026-09-10's log shows
URBANCO re-logged as a fresh "LIVE REQUALIFIED" admission 69 times across
5.5 hours of continuous trading — once every ~287 seconds, matching this
300s cycle almost exactly — while producing zero `intraday_setups` rows all
day, alongside three other real, liquid, static-gate-clearing NIFTY 500
movers (AFCONS, BEML, EIDPARRY) admitted the same way with the same result.

These tests pin the two pure merge functions directly: a live-requalified
bench entry / bench-only tick-built context survives a rebuild that does not
reproduce it, the static rebuild still WINS when it does reproduce a symbol
(no stale duplicate), a symbol that has genuinely dropped off the bench is
correctly NOT carried forever, and a same-day, mid-session sequence of
(admit -> rebuild -> re-admit attempt) leaves exactly one entry, not a
duplicate — reproducing the 69x-relog symptom as a regression test.
"""

from __future__ import annotations


def _entry(symbol, source="bench", score=0.5):
    from intraday.scanner import UniverseEntry
    return UniverseEntry(symbol=symbol, close=100.0, value_cr=50.0,
                         atr_pct=2.0, delivery_pct=30.0, sector="X",
                         score=score, reason="", avg_vol_20d=100_000.0,
                         source=source)


# ── _merge_carried_bench ─────────────────────────────────────────────────

def test_live_requalified_entry_survives_a_rebuild_that_does_not_reproduce_it():
    from intraday.engine import _merge_carried_bench
    fresh = [_entry("RELIANCE"), _entry("TCS")]           # this cycle's static rescore
    existing = [_entry("RELIANCE"), _entry("URBANCO", source="population_d")]
    merged = _merge_carried_bench(fresh, existing)
    symbols = [e.symbol for e in merged]
    assert symbols == ["RELIANCE", "TCS", "URBANCO"], (
        "URBANCO was live-requalified (source != 'bench') and the fresh "
        "static rescore has no way to reproduce it — it must be carried "
        "forward, not silently dropped on this rebuild")


def test_stale_static_entry_is_not_carried_once_the_fresh_scan_drops_it():
    from intraday.engine import _merge_carried_bench
    fresh = [_entry("RELIANCE")]                           # TCS fell out of today's scan
    existing = [_entry("RELIANCE"), _entry("TCS", source="bench")]
    merged = _merge_carried_bench(fresh, existing)
    symbols = [e.symbol for e in merged]
    assert symbols == ["RELIANCE"], (
        "TCS's old entry has source=='bench' — it is the STATIC scan's own "
        "prior output, not a live admission, and must be superseded by the "
        "fresh scan like today, not carried forward forever")


def test_fresh_scan_wins_when_it_reproduces_the_same_symbol_no_duplicate():
    from intraday.engine import _merge_carried_bench
    fresh = [_entry("URBANCO", source="bench")]  # today it qualified outright
    existing = [_entry("URBANCO", source="population_d")]  # yesterday it was requalified
    merged = _merge_carried_bench(fresh, existing)
    assert len(merged) == 1 and merged[0] is fresh[0], (
        "when the fresh static scan reproduces a symbol itself, that entry "
        "wins — no stale duplicate carried alongside it")


def test_repeated_rebuild_between_requalify_admissions_produces_no_duplicates():
    """
    Reproduces the exact live symptom as a regression test: admit once,
    rebuild (simulating refresh_universe()'s 300s tick) without the fresh
    scan reproducing the name, "admit" again (simulating live_requalify_
    universe() re-appending because its own `existing` snapshot was taken
    AFTER the rebuild) — must settle at exactly one entry, not two.
    """
    from intraday.engine import _merge_carried_bench
    bench = [_entry("RELIANCE", source="bench")]
    admitted = _entry("URBANCO", source="population_d")
    bench = bench + [admitted]                              # live_requalify_universe() appends
    fresh = [_entry("RELIANCE", source="bench")]             # 300s tick: static rescore
    bench = _merge_carried_bench(fresh, bench)
    assert [e.symbol for e in bench] == ["RELIANCE", "URBANCO"]
    # a second requalify pass sees URBANCO already present and does not re-append
    existing_symbols = {e.symbol for e in bench}
    assert "URBANCO" in existing_symbols, (
        "with the fix, URBANCO stays visible to live_requalify_universe()'s "
        "own `existing = {e.symbol for e in self._bench}` check across the "
        "rebuild, so it is correctly excluded from re-detection — before "
        "this fix it dropped out every ~300s and was re-admitted from "
        "scratch, which is what produced 69 duplicate log lines for one "
        "real symbol on 2026-09-10")


# ── _merge_carried_contexts ──────────────────────────────────────────────

def _ctx(symbol, live_fields=()):
    from intraday.strategies.base import SymbolContext
    return SymbolContext(symbol=symbol, ltp=100.0, bars=[], live_fields=live_fields)


def test_bench_only_tick_built_context_survives_a_rebuild_that_does_not_cover_it():
    from intraday.engine import _merge_carried_contexts
    built = {"RELIANCE": _ctx("RELIANCE")}                  # this cycle's historical-bar contexts
    previous = {"RELIANCE": _ctx("RELIANCE"),
                "URBANCO": _ctx("URBANCO", live_fields=("bars",))}
    merged = _merge_carried_contexts(built, previous, bench_symbols={"RELIANCE", "URBANCO"})
    assert set(merged) == {"RELIANCE", "URBANCO"}
    assert merged["URBANCO"] is previous["URBANCO"], (
        "URBANCO's only context is the one merge_live_bars() tick-built — "
        "context_symbols() never covers a bench-only name, so it must be "
        "carried forward, not erased by this rebuild")


def test_context_is_not_carried_once_its_symbol_leaves_the_bench():
    from intraday.engine import _merge_carried_contexts
    built = {"RELIANCE": _ctx("RELIANCE")}
    previous = {"RELIANCE": _ctx("RELIANCE"),
                "URBANCO": _ctx("URBANCO", live_fields=("bars",))}
    # URBANCO has genuinely fallen off the bench this cycle
    merged = _merge_carried_contexts(built, previous, bench_symbols={"RELIANCE"})
    assert set(merged) == {"RELIANCE"}, (
        "a symbol no longer in self._bench must not leak a stale context "
        "forever just because it once had one")


def test_fresh_built_context_wins_over_a_carried_one_for_the_same_symbol():
    from intraday.engine import _merge_carried_contexts
    fresh = _ctx("URBANCO")
    built = {"URBANCO": fresh}                              # URBANCO made the top-40 this cycle
    previous = {"URBANCO": _ctx("URBANCO", live_fields=("bars",))}
    merged = _merge_carried_contexts(built, previous, bench_symbols={"URBANCO"})
    assert merged["URBANCO"] is fresh, (
        "a real historical-bar context always supersedes a carried, "
        "tick-only one for the same symbol")


TESTS = [
    ("live-requalified bench entry survives a non-reproducing rebuild",
     test_live_requalified_entry_survives_a_rebuild_that_does_not_reproduce_it),
    ("stale static bench entry is not carried once dropped",
     test_stale_static_entry_is_not_carried_once_the_fresh_scan_drops_it),
    ("fresh bench scan wins over a carried duplicate",
     test_fresh_scan_wins_when_it_reproduces_the_same_symbol_no_duplicate),
    ("repeated bench rebuild between requalify passes: no duplicates",
     test_repeated_rebuild_between_requalify_admissions_produces_no_duplicates),
    ("bench-only tick-built context survives a non-covering rebuild",
     test_bench_only_tick_built_context_survives_a_rebuild_that_does_not_cover_it),
    ("context not carried once its symbol leaves the bench",
     test_context_is_not_carried_once_its_symbol_leaves_the_bench),
    ("fresh built context wins over a carried one",
     test_fresh_built_context_wins_over_a_carried_one_for_the_same_symbol),
]
