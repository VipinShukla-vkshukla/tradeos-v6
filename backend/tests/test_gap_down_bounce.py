"""
GDB (Gap-Down Bounce) — new engine from brain_proposals#190 ("UNSEEN/gap
down > 1%"), built 11-Aug-2026. See intraday/strategies/gap_down_bounce.py's
own docstring for the full evidence and reasoning; this pins the mechanics
and — the part most worth getting wrong quietly — the SHADOW launch.

Reuses vwap_reclaim.py's reclaim logic (including its 10-Aug-2026 freshness
fix), pointed at the population VWR explicitly refuses (still negative on
the day). Two of these tests exist specifically to confirm that fix carried
over correctly rather than being silently reintroduced in the copy.
"""

from __future__ import annotations

from config import cfg_float
from intraday.session import OPENING, PRIME, DRIFT
from intraday.strategies.base import SymbolContext
from tests import cfg_ctx
from tests._fixtures import OPEN, bars as _bars


def _ctx(closes, ltp, prev_close, day_high=None, vwap=98.9):
    bs = _bars([(c - 0.3, c + 0.3, c - 0.4, c, 20_000) for c in closes])
    return SymbolContext(
        symbol="GDBCO", ltp=ltp, bars=bs, vwap=vwap,
        day_open=closes[0],
        day_high=day_high if day_high is not None else max(b.high for b in bs),
        day_low=min(b.low for b in bs), prev_close=prev_close,
        prev_high=prev_close, prev_low=prev_close - 2,
        atr_pct_daily=2.0, avg_volume_20d=4_000_000, value_cr=150.0,
        sector="IT", rs_vs_index_pct=-0.5, as_of=OPEN,
    )


# A gap-down-then-bounce shape: opens ~1.5% below prev_close, dips further,
# reclaims VWAP on the final bar. day_open = closes[0] = 99.0, prev_close
# 100.5 -> gap ~-1.49%, comfortably past the 1% minimum and well inside the
# 4% exhaustion ceiling (atr 2.0% x gdb_max_gap_atr_frac 2.0).
_FRESH_CLOSES = [99.0, 98.8, 98.6, 98.5, 98.4, 98.3, 98.3, 98.4, 98.5, 98.6, 98.7, 99.1]
_PREV_CLOSE = 100.5


def test_fresh_gap_down_bounce_fires_long():
    from intraday.strategies.gap_down_bounce import GapDownBounce
    ctx = _ctx(_FRESH_CLOSES, ltp=99.15, prev_close=_PREV_CLOSE)
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, OPENING)
    assert s is not None, "a genuine gap-down + fresh VWAP reclaim must fire"
    assert s.direction == "LONG"
    ok, why = s.coherent()
    assert ok, f"engine emitted incoherent levels — {why}"
    assert s.meta["source_proposal"] == "brain_proposals#190"


def test_fires_in_prime_too_not_only_opening():
    from intraday.strategies.gap_down_bounce import GapDownBounce
    ctx = _ctx(_FRESH_CLOSES, ltp=99.15, prev_close=_PREV_CLOSE)
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, PRIME)
    assert s is not None


def test_refuses_outside_its_declared_phases():
    from intraday.strategies.gap_down_bounce import GapDownBounce
    ctx = _ctx(_FRESH_CLOSES, ltp=99.15, prev_close=_PREV_CLOSE)
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, DRIFT)
    assert s is None


def test_gap_smaller_than_threshold_is_refused():
    """
    Down only ~0.3% — not a gap by this engine's own definition. Otherwise
    shaped EXACTLY like the fresh-reclaim happy path (crossing on the last
    bar, real time spent below VWAP first, ample volume) so the ONLY thing
    that can refuse this is the gap-size guard, not an accidental collision
    with the freshness or volume checks.

    Caught the hard way: the first version of this fixture had its second-
    to-last close already above VWAP, so it was silently being refused by
    the CROSSING guard instead — the test passed for the wrong reason and
    said nothing when the gap-size guard was disabled entirely.
    """
    from intraday.strategies.gap_down_bounce import GapDownBounce
    small_gap_closes = [99.9, 99.6, 99.5, 99.4, 99.4, 99.5, 99.6, 99.65, 99.7, 99.75, 99.7, 100.0]
    ctx = _ctx(small_gap_closes, ltp=100.05, prev_close=100.2, vwap=99.9)
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, OPENING)
    assert s is None, "a 0.3% gap must not qualify as 'gap down > 1%'"


def test_exhaustion_gap_beyond_the_ceiling_is_refused():
    """Down ~9% — a crash gap, a different regime than overnight noise."""
    from intraday.strategies.gap_down_bounce import GapDownBounce
    crash_closes = [91.0, 90.5, 90.0, 89.8, 89.7, 89.8, 90.0, 90.3, 90.6, 90.9, 91.2, 91.6]
    ctx = _ctx(crash_closes, ltp=91.7, prev_close=100.0, vwap=90.5)
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, OPENING)
    assert s is None, "an exhaustion-scale gap must not be read as a routine bounce setup"


def test_stale_reclaim_several_bars_old_is_refused():
    """Same shape as VWR's own 10-Aug freshness bug, replayed here to confirm
    the fix carried over into the copy rather than being silently dropped."""
    from intraday.strategies.gap_down_bounce import GapDownBounce
    closes = [98.0, 98.0, 98.0, 98.0, 98.0, 98.0, 98.5, 98.5, 99.1, 99.15, 99.2, 99.25]
    ctx = _ctx(closes, ltp=99.3, prev_close=100.5, vwap=99.0)
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, OPENING)
    assert s is None, "a reclaim that crossed several bars ago must be refused, not chased"


def test_still_below_vwap_is_refused():
    from intraday.strategies.gap_down_bounce import GapDownBounce
    closes = [99.0] * 11 + [99.3]  # never closes above vwap=99.5
    ctx = _ctx(closes, ltp=99.6, prev_close=100.5, vwap=99.5)  # ltp above, bars are not
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, OPENING)
    assert s is None


def test_thin_volume_bounce_is_refused():
    from intraday.strategies.gap_down_bounce import GapDownBounce
    bs = _bars([(c - 0.3, c + 0.3, c - 0.4, c, 500) for c in _FRESH_CLOSES])  # thin
    ctx = SymbolContext(
        symbol="GDBCO", ltp=99.15, bars=bs, vwap=98.9, day_open=_FRESH_CLOSES[0],
        day_high=max(b.high for b in bs), day_low=min(b.low for b in bs),
        prev_close=_PREV_CLOSE, prev_high=_PREV_CLOSE, prev_low=_PREV_CLOSE - 2,
        atr_pct_daily=2.0, avg_volume_20d=4_000_000, value_cr=150.0,
        sector="IT", rs_vs_index_pct=-0.5, as_of=OPEN,
    )
    with cfg_ctx({}):
        s = GapDownBounce().evaluate(ctx, OPENING)
    assert s is None, "a bounce on thin volume is a drift back to average, not real buying"


# ── wiring: registered, and launched SHADOW — not the registry's own default

def test_registered_in_the_engine_list():
    from intraday.strategies.registry import engine_names
    assert "GDB" in engine_names()


def test_unconfigured_lifecycle_would_default_active_which_is_why_the_row_matters():
    """Documents registry.py's own stated default (fail-open to ACTIVE for a
    forgotten config row) -- the exact reason GDB's system_config row must be
    seeded explicitly at SHADOW rather than left to that default. See
    migration 076."""
    from intraday.strategies.registry import engine_lifecycle, LIFECYCLE_ACTIVE
    with cfg_ctx({}):  # no intraday_engine_gdb_lifecycle key at all
        assert engine_lifecycle("GDB") == LIFECYCLE_ACTIVE


def test_configured_shadow_is_honoured():
    from intraday.strategies.registry import engine_lifecycle, LIFECYCLE_SHADOW
    with cfg_ctx({"intraday_engine_gdb_lifecycle": "SHADOW"}):
        assert engine_lifecycle("GDB") == LIFECYCLE_SHADOW


def test_shadow_engine_evaluates_and_is_returned_but_never_fundable():
    """The whole point of SHADOW: it still runs and gets recorded, it just
    cannot win evaluate_all()'s `best` slot."""
    from intraday.strategies.registry import evaluate_all
    ctx = _ctx(_FRESH_CLOSES, ltp=99.15, prev_close=_PREV_CLOSE)
    with cfg_ctx({"intraday_engine_gdb_lifecycle": "SHADOW",
                  # Silence every other engine so GDB's own setup is the only
                  # thing that could possibly appear in `found`.
                  "intraday_engine_orb_enabled": "false",
                  "intraday_engine_gap_enabled": "false",
                  "intraday_engine_pdl_enabled": "false",
                  "intraday_engine_vce_enabled": "false",
                  "intraday_engine_pbk_enabled": "false",
                  "intraday_engine_vwr_enabled": "false",
                  "intraday_engine_rng_enabled": "false",
                  "intraday_engine_sdn_enabled": "false"}):
        best, found = evaluate_all(ctx, OPENING)
    assert any(s.strategy == "GDB" for s in found), "SHADOW must still evaluate and record"
    assert best is None, "a SHADOW-only detection must never be fundable"


TESTS = [
    ("fresh gap-down bounce fires LONG", test_fresh_gap_down_bounce_fires_long),
    ("fires in PRIME too, not only OPENING", test_fires_in_prime_too_not_only_opening),
    ("refuses outside its declared phases", test_refuses_outside_its_declared_phases),
    ("gap smaller than threshold is refused", test_gap_smaller_than_threshold_is_refused),
    ("exhaustion gap beyond the ceiling is refused",
     test_exhaustion_gap_beyond_the_ceiling_is_refused),
    ("stale reclaim several bars old is refused",
     test_stale_reclaim_several_bars_old_is_refused),
    ("still below VWAP is refused", test_still_below_vwap_is_refused),
    ("thin volume bounce is refused", test_thin_volume_bounce_is_refused),
    ("registered in the engine list", test_registered_in_the_engine_list),
    ("unconfigured lifecycle would default ACTIVE — why the row matters",
     test_unconfigured_lifecycle_would_default_active_which_is_why_the_row_matters),
    ("configured SHADOW is honoured", test_configured_shadow_is_honoured),
    ("SHADOW engine evaluates and is returned but never fundable",
     test_shadow_engine_evaluates_and_is_returned_but_never_fundable),
]
