"""
VWAP reclaim (VWR) refinement, not retirement (10-Aug-2026).

WHY REFINE RATHER THAN RETIRE
------------------------------
engine_scorecard.py measured VWR at n=34, gross -0.059R — "NO EDGE, signal
problem". The operator's instruction was explicit: refine it and confirm
there IS no fix before recommending retirement, not retire by default.
Reading the engine's own logic against its own comments found two concrete
bugs, not a fundamentally broken idea:

1. THE FRESHNESS CHECK WAS A NO-OP IN THE COMMON CASE. The code claimed
   "current bar must be above and the prior one below — the crossing itself"
   but the actual condition let a reclaim that happened several bars back
   pass, because its fallback branch only required ANY bar in a 3-bar window
   to have been below VWAP — almost always true once min_below (3) is
   already satisfied over the wider 12-bar lookback. VWR was chasing
   established moves, the exact failure mode its own docstring names.

2. THE TARGET COULD BE SET PAST A LEVEL THE STOCK HAD NEVER TRADED AT.
   `max(by_r, day_high*1.001)` picks whichever is LARGER — so whenever the
   2R measure exceeded day_high (the common shape: a reclaim as a second,
   smaller leg off a high made earlier), the target silently became a level
   with no precedent today, rather than the proven high the docstring says
   it prefers. exit_policy.py's use_setup_target rule exits AT this field
   when price reaches it — an unreachable target is a trade that rides the
   give-back guard down instead of banking a real, achievable move, which
   is this project's own recurring finding this session.

These tests pin both fixes and confirm the happy path (a genuinely fresh
reclaim) still fires.
"""

from __future__ import annotations

from config import cfg_float
from intraday.session import PRIME
from intraday.strategies.base import SymbolContext
from tests import cfg_ctx
from tests._fixtures import OPEN, bars as _bars


def _ctx(closes, ltp, day_high=None, vwap=100.0, prev_close=99.5, rs=0.5):
    # LOW OFFSET 0.10, NOT 0.40 -- 18-Aug-2026. A 0.4 low on a Rs 100 stock is
    # a 0.7%-range minute bar, which put the swing low 1.3% under LTP and made
    # every fixture here unaffordable under vwr_max_risk_pct=0.90. That went
    # unnoticed while the engine silently clamped the stop; with the clamp
    # removed (base.risk_from_structure) the fixture, not the engine, is what
    # fails. These bars are now shaped like the reclaim the engine trades.
    bs = _bars([(c - 0.08, c + 0.08, c - 0.10, c, 10_000) for c in closes])
    return SymbolContext(
        symbol="VWRCO", ltp=ltp, bars=bs, vwap=vwap,
        day_open=closes[0],
        day_high=day_high if day_high is not None else max(b.high for b in bs),
        day_low=min(b.low for b in bs), prev_close=prev_close,
        prev_high=prev_close, prev_low=prev_close - 2,
        atr_pct_daily=2.0, avg_volume_20d=4_000_000, value_cr=150.0,
        sector="IT", rs_vs_index_pct=rs, as_of=OPEN,
    )


# ── freshness check ───────────────────────────────────────────────────────

def test_a_reclaim_that_happened_several_bars_ago_is_refused():
    """Traced by hand from the real bug: 6 bars of noise below VWAP, then a
    crossing at bar -4 -> -3 (99 -> 100.1), followed by two more bars still
    above VWAP. The reclaim itself is THREE bars old by the time this
    evaluates, not fresh -- and stays within the extension cap (kept close to
    VWAP throughout) so it is the freshness check being pinned here, not the
    extension one. The pre-fix logic let this through: its fallback branch
    only required ANY bar in a 3-bar window to have been below VWAP, which
    9 bars of prior noise trivially satisfied."""
    from intraday.strategies.vwap_reclaim import VwapReclaim
    closes = [99.4, 99.4, 99.4, 99.4, 99.4, 99.4, 99.6, 99.6, 99.8, 100.1, 100.15, 100.2]
    ctx = _ctx(closes, ltp=100.25)
    with cfg_ctx({}):
        s = VwapReclaim().evaluate(ctx, PRIME)
    assert s is None, "a reclaim that crossed several bars ago must be refused, not chased"


def test_a_fresh_single_bar_crossing_still_fires():
    """The happy path this engine exists for must survive the fix."""
    from intraday.strategies.vwap_reclaim import VwapReclaim
    closes = [99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.85, 99.9, 100.2]
    ctx = _ctx(closes, ltp=100.3)
    with cfg_ctx({}):
        s = VwapReclaim().evaluate(ctx, PRIME)
    assert s is not None, "a genuine fresh reclaim (last bar above, prior bar below) must still fire"
    assert s.direction == "LONG"
    ok, why = s.coherent()
    assert ok, f"engine emitted incoherent levels — {why}"


def test_still_below_vwap_on_the_last_bar_is_refused():
    """Regression guard: the OTHER branch of the old OR (last bar still
    below VWAP) must keep refusing, unchanged by this fix."""
    from intraday.strategies.vwap_reclaim import VwapReclaim
    closes = [99.8] * 11 + [99.9]  # never actually closes above vwap=100
    ctx = _ctx(closes, ltp=100.2)  # ltp itself is above, bars are not
    with cfg_ctx({}):
        s = VwapReclaim().evaluate(ctx, PRIME)
    assert s is None


# ── target construction ────────────────────────────────────────────────────

_FRESH_CLOSES = [99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.8, 99.85, 99.9, 100.2]


def test_target_prefers_a_worthwhile_day_high_over_the_r_multiple():
    from intraday.strategies.vwap_reclaim import VwapReclaim
    eng = VwapReclaim()
    with cfg_ctx({}):
        baseline = eng.evaluate(_ctx(_FRESH_CLOSES, ltp=100.3, day_high=None), PRIME)
    assert baseline is not None
    # Far above entry -- guaranteed to clear vwr_min_target_r regardless of
    # exactly what the (clamped) risk works out to.
    far_high = baseline.entry * 1.5
    with cfg_ctx({}):
        s = eng.evaluate(_ctx(_FRESH_CLOSES, ltp=100.3, day_high=far_high), PRIME)
    assert s is not None
    assert abs(s.target - round(far_high * 1.001, 2)) < 0.05, (
        f"expected the target at the proven day high ({far_high * 1.001:.2f}); "
        f"got {s.target}")


def test_target_uses_a_partial_day_high_instead_of_overshooting_past_it():
    """THE bug being fixed. day_high between 1R (vwr_min_target_r, worthwhile)
    and 2R (vwr_target_r, the by_r fallback) above entry — the "second,
    smaller leg" shape — used to be DISCARDED in favour of the unproven 2R
    target, because max(by_r, day_high*1.001) always picks the larger number
    regardless of which one the stock actually proved it could reach. Traced
    against the OLD code by hand: this exact case (partial_high = entry +
    1.5R) returns by_r under max(), silently overshooting a real level."""
    from intraday.strategies.vwap_reclaim import VwapReclaim
    eng = VwapReclaim()
    with cfg_ctx({}):
        baseline = eng.evaluate(_ctx(_FRESH_CLOSES, ltp=100.3, day_high=0.0), PRIME)
    assert baseline is not None
    risk = baseline.entry - baseline.stop
    assert risk > 0
    partial_high = baseline.entry + risk * 1.5   # between 1R and 2R
    with cfg_ctx({}):
        s = eng.evaluate(_ctx(_FRESH_CLOSES, ltp=100.3, day_high=partial_high), PRIME)
    assert s is not None
    assert abs(s.target - round(partial_high * 1.001, 2)) < 0.05, (
        f"expected the target at the proven (partial) day high "
        f"{partial_high * 1.001:.2f}, not an unproven R-multiple; got {s.target}")


def test_target_falls_back_when_there_is_no_day_high_at_all():
    from intraday.strategies.vwap_reclaim import VwapReclaim
    with cfg_ctx({}):
        s = VwapReclaim().evaluate(_ctx(_FRESH_CLOSES, ltp=100.3, day_high=0.0), PRIME)
    assert s is not None
    risk = s.entry - s.stop
    expected = round(s.entry + risk * cfg_float("vwr_target_r", 2.0), 2)
    assert abs(s.target - expected) < 0.05


TESTS = [
    ("a reclaim that happened several bars ago is refused",
     test_a_reclaim_that_happened_several_bars_ago_is_refused),
    ("a fresh single-bar crossing still fires",
     test_a_fresh_single_bar_crossing_still_fires),
    ("still below VWAP on the last bar is refused",
     test_still_below_vwap_on_the_last_bar_is_refused),
    ("target prefers a worthwhile day high over the R-multiple",
     test_target_prefers_a_worthwhile_day_high_over_the_r_multiple),
    ("target uses a partial day high instead of overshooting past it",
     test_target_uses_a_partial_day_high_instead_of_overshooting_past_it),
    ("target falls back when there is no day high at all",
     test_target_falls_back_when_there_is_no_day_high_at_all),
]
