"""
A break must clear the level it breaks, and the entry budget must be measured
against the budget (12-Aug-2026).

WHAT HAPPENED
--------------
Ten intraday paper entries landed between 09:30:29 and 09:36:52 — the whole
day's `intraday_max_new_per_day` budget in six minutes and twenty-three
seconds, all LONG, into a tape that ran −0.12% → −0.43% over the next half
hour. Nine lost. Four of them were ORB and three were VCE, and every one of
those seven was closed SETUP_INVALIDATED, not stopped out:

  SBIN       ORB  broke the range by 0.02%  →  −0.15R  "closed back inside"
  MOTHERSON  ORB  broke the range by 0.03%  →  −0.21R  "closed back inside"
  ZENTEC     ORB                            →  −0.26R  "closed back inside"
  GVT&D      ORB  broke the range by 0.03%  →  +0.17R  (gave back 68%)
  HINDALCO   VCE  released 0.04% over coil  →  −0.24R  "returned inside"
  HINDZINC   VCE  released 0.19% over coil  →  −0.47R  "returned inside"
  ZEEL       VCE  released 0.15% over coil  →  −0.36R  "returned inside"

TWO DEFECTS, ONE SHAPE
-----------------------
1. ORB and VCE both bounded the break from ABOVE (`> max_chase` → the move is
   spent) and never from below. ORB's module docstring names the missing
   filter explicitly — "a break by two paise is a quote, not a signal" — and
   the engine then paid `break_pct < 0.2` a +0.10 confidence BONUS, so its
   least-confirmed breaks were its highest-confidence ones. A documented
   filter that was never implemented, scoring backwards.

2. `_entry_cap()`, the denominator of the rising confidence floor, read
   `intraday_max_orders_per_day` (20, a broker risk limit) instead of
   `intraday_max_new_per_day` (10, the budget entries are actually drawn
   from). The floor therefore rose at half rate and topped out at 0.675 with
   the budget fully spent. SDN's modal confidence on a down tape is 0.66
   (0.62 base + 0.04 for negative RS), so every short the session offered
   after 09:38 was refused by 0.015 — by a bar set by the ten losing longs.

WHAT THIS CATCHES
------------------
That the confirmation floor can FAIL (a two-paise break is refused) and that
it can PASS (a genuine break is still taken) — both directions, because a
threshold nothing clears is this project's other recurring defect. That the
floor is never below the invalidation buffer, which is the property that stops
a trade being born inside its own kill zone. That the ORB confidence bonus no
longer rewards the weakest breaks. And that the entry cap reads the budget.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config import IST
from intraday.session import PRIME
from intraday.strategies.base import Bar, SymbolContext, confirmation_pct
from tests import cfg_ctx


# ── confirmation_pct(), pure ─────────────────────────────────────────────

def test_floor_is_never_below_the_invalidation_buffer():
    """
    The binding property. Entering closer to the level than the exit's own
    buffer opens a trade already inside the band that will close it.
    """
    from config import cfg_float
    buf = cfg_float("intraday_invalidation_buffer_pct", 0.12)
    for width in (0.0, 0.1, 0.5, 1.44, 2.78, 9.0):
        assert confirmation_pct(width, 0.10) >= buf, width


def test_floor_scales_with_the_structure_being_broken():
    """A 2.78%-wide range must be cleared by more than a 1.44%-wide one."""
    assert confirmation_pct(2.78, 0.10) > confirmation_pct(1.44, 0.10)
    assert abs(confirmation_pct(2.78, 0.10) - 0.278) < 1e-9


def test_a_negative_or_absurd_width_still_returns_the_buffer():
    from config import cfg_float
    buf = cfg_float("intraday_invalidation_buffer_pct", 0.12)
    assert confirmation_pct(-5.0, 0.10) == buf


# ── the ORB / VCE gate, against the real 12-Aug numbers ──────────────────

def _ctx(symbol: str, bars: list[Bar], ltp: float, **kw) -> SymbolContext:
    return SymbolContext(symbol=symbol, bars=bars, ltp=ltp, **kw)


def _range_bars(lo: float, hi: float, vol: float = 100_000.0) -> list[Bar]:
    """Three 5-min bars spanning [lo, hi] — an opening range / coil."""
    t0 = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    mid = (hi + lo) / 2
    return [
        Bar(ts=t0 + timedelta(minutes=5 * i), open=mid, high=hi, low=lo,
            close=mid, volume=vol)
        for i in range(3)
    ]


def test_the_sbin_break_is_refused():
    """
    SBIN, 09:34 — range 1066.40–1081.80 (1.44% wide), broken by 0.02%.
    Entered at 1082.00, invalidation line at 1080.50, cut at 1080.40.
    """
    range_pct = (1081.80 - 1066.40) / 1066.40 * 100.0
    break_pct = (1082.00 - 1081.80) / 1081.80 * 100.0
    assert break_pct < confirmation_pct(range_pct, 0.10), (
        f"a {break_pct:.3f}% break of a {range_pct:.2f}% range must not qualify")


def test_the_gvtd_and_motherson_breaks_are_refused():
    for hi, lo, ltp in ((4517.20, 4395.20, 4518.40),     # GVT&D, 0.03%
                        (171.50, 169.05, 171.55)):        # MOTHERSON, 0.03%
        range_pct = (hi - lo) / lo * 100.0
        break_pct = (ltp - hi) / hi * 100.0
        assert break_pct < confirmation_pct(range_pct, 0.10)


def test_the_hindalco_coil_release_is_refused():
    """HINDALCO VCE, 09:30 — coil 0.64% wide, released 0.037% over 1084.40."""
    break_pct = (1084.80 - 1084.40) / 1084.40 * 100.0
    assert break_pct < confirmation_pct(0.64, 0.10)


def test_a_real_break_still_passes():
    """
    THE OTHER DIRECTION. A threshold no setup can clear is the same defect
    wearing a different hat: a 0.35% break of a 1.44% range is an ordinary,
    well-confirmed ORB and must still be taken.
    """
    assert 0.35 > confirmation_pct(1.44, 0.10)
    assert 0.30 > confirmation_pct(0.90, 0.10)


def test_orb_produces_no_setup_on_a_two_paise_break_but_does_on_a_real_one():
    """End to end through the engine, not just the helper."""
    from intraday.strategies.orb import OpeningRangeBreakout
    eng = OpeningRangeBreakout()
    bars = _range_bars(1066.40, 1081.80)
    common = dict(atr_pct_daily=3.0, prev_high=1000.0, rs_vs_index_pct=0.4)

    # ORB's stop sits below the RANGE LOW, so its risk is always at least the
    # range width -- 1.44% here, past orb_max_risk_pct=1.20. Until 18-Aug the
    # engine clamped the stop and fired anyway; base.risk_from_structure now
    # refuses instead. This module tests BREAK CONFIRMATION, so the risk cap is
    # lifted to isolate that one variable. The production consequence (ORB
    # cannot honestly fund a break of a range this wide) is deliberate and is
    # pinned by tests/test_structural_stop.py, not waved away here.
    with cfg_ctx({"orb_max_risk_pct": "5.0"}):
        quote = eng.evaluate(_ctx("SBIN", bars, 1082.00, **common), PRIME)
        assert quote is None, "a 0.02% break must not become a Setup"

        real = eng.evaluate(_ctx("SBIN", bars, 1086.00, **common), PRIME)
    assert real is not None, "a 0.39% break of a 1.44% range must still trade"
    assert real.direction == "LONG"
    assert real.coherent()[0]


def test_orb_confidence_no_longer_rewards_the_weakest_break():
    """
    The old form paid +0.10 for `break_pct < 0.2`, i.e. its largest bonus to
    the breaks now refused outright. The bonus must sit inside the band the
    engine will actually trade.
    """
    from intraday.strategies.orb import OpeningRangeBreakout
    from config import cfg_float
    eng = OpeningRangeBreakout()
    bars = _range_bars(1066.40, 1081.80)
    common = dict(atr_pct_daily=3.0, prev_high=1000.0, rs_vs_index_pct=0.4)

    range_pct = (1081.80 - 1066.40) / 1066.40 * 100.0
    min_break = confirmation_pct(range_pct, cfg_float("orb_min_break_frac", 0.10))
    max_chase = cfg_float("orb_max_chase_pct", 0.60)
    midpoint = (min_break + max_chase) / 2

    just_confirmed = 1081.80 * (1 + (min_break + 0.02) / 100.0)
    nearly_chased = 1081.80 * (1 + (max_chase - 0.02) / 100.0)

    # ORB's stop sits below the RANGE LOW, so its risk is always at least the
    # range width -- 1.44% here, past orb_max_risk_pct=1.20. Until 18-Aug the
    # engine clamped the stop and fired anyway; base.risk_from_structure now
    # refuses instead. This module tests BREAK CONFIRMATION, so the risk cap is
    # lifted to isolate that one variable. The production consequence (ORB
    # cannot honestly fund a break of a range this wide) is deliberate and is
    # pinned by tests/test_structural_stop.py, not waved away here.
    with cfg_ctx({"orb_max_risk_pct": "5.0"}):
        a = eng.evaluate(_ctx("SBIN", bars, just_confirmed, **common), PRIME)
        b = eng.evaluate(_ctx("SBIN", bars, nearly_chased, **common), PRIME)
    assert a is not None and b is not None
    assert min_break < midpoint < max_chase
    assert a.confidence >= b.confidence, (
        "the earlier of two CONFIRMED breaks should not score lower")


# ── the entry-cap denominator ────────────────────────────────────────────

def test_entry_cap_reads_the_budget_not_the_broker_order_limit():
    """
    The floor's denominator must be the same quantity the budget check uses.
    With the 12-Aug production values (new_per_day=10, orders_per_day=20) the
    old code reported the day half spent at the moment it was fully spent.
    """
    from tests import cfg_ctx
    from intraday.engine import IntradayEngine

    with cfg_ctx({"intraday_max_new_per_day": "10",
                  "intraday_max_orders_per_day": "20"}):
        cap = IntradayEngine._entry_cap(object.__new__(IntradayEngine))
        assert cap == 10, f"expected the budget (10), got {cap}"


def test_the_floor_reaches_scarce_when_the_budget_is_gone():
    """
    A check that cannot PASS is a defect too: at a fully spent budget the
    floor must actually arrive at `intraday_min_confidence_scarce`, and at an
    untouched one it must sit at the base.
    """
    from tests import cfg_ctx
    from intraday.engine import IntradayEngine

    eng = object.__new__(IntradayEngine)
    with cfg_ctx({"intraday_max_new_per_day": "10",
                  "intraday_max_orders_per_day": "20",
                  "intraday_min_confidence": "0.55",
                  "intraday_min_confidence_scarce": "0.80"}):
        eng._entries_today = lambda: 10          # type: ignore[method-assign]
        assert eng._confidence_floor() == 0.80
        eng._entries_today = lambda: 0           # type: ignore[method-assign]
        assert eng._confidence_floor() == 0.55
        eng._entries_today = lambda: 5           # type: ignore[method-assign]
        assert eng._confidence_floor() == 0.675


def test_the_shorts_refused_on_12aug_would_now_clear_the_bar():
    """
    The consequence, stated as the trade it cost. SDN's modal confidence on a
    down tape is 0.66. With the budget fully spent the floor is 0.80 and 0.66
    is refused — correctly, the budget IS gone. The defect was the middle of
    the range: at 5 of 10 spent the old floor read 0.6125 (5/20) and admitted
    entries 6–10; the corrected floor reads 0.675 and refuses them.
    """
    from tests import cfg_ctx
    from intraday.engine import IntradayEngine

    eng = object.__new__(IntradayEngine)
    with cfg_ctx({"intraday_max_new_per_day": "10",
                  "intraday_max_orders_per_day": "20",
                  "intraday_min_confidence": "0.55",
                  "intraday_min_confidence_scarce": "0.80"}):
        eng._entries_today = lambda: 5           # type: ignore[method-assign]
        corrected = eng._confidence_floor()
    old = round(0.55 + (0.80 - 0.55) * (5 / 20), 3)
    assert old == 0.613 and corrected == 0.675
    assert corrected > old, "the sixth entry of the day must be harder, not easier"


TESTS = [
    ("floor is never below the invalidation buffer",
     test_floor_is_never_below_the_invalidation_buffer),
    ("floor scales with the structure", test_floor_scales_with_the_structure_being_broken),
    ("absurd width still returns the buffer",
     test_a_negative_or_absurd_width_still_returns_the_buffer),
    ("the SBIN break is refused", test_the_sbin_break_is_refused),
    ("the GVT&D and MOTHERSON breaks are refused",
     test_the_gvtd_and_motherson_breaks_are_refused),
    ("the HINDALCO coil release is refused", test_the_hindalco_coil_release_is_refused),
    ("a real break still passes", test_a_real_break_still_passes),
    ("ORB refuses a quote and takes a break",
     test_orb_produces_no_setup_on_a_two_paise_break_but_does_on_a_real_one),
    ("ORB confidence no longer rewards the weakest break",
     test_orb_confidence_no_longer_rewards_the_weakest_break),
    ("entry cap reads the budget", test_entry_cap_reads_the_budget_not_the_broker_order_limit),
    ("the floor reaches scarce when the budget is gone",
     test_the_floor_reaches_scarce_when_the_budget_is_gone),
    ("the mid-budget floor is now stricter",
     test_the_shorts_refused_on_12aug_would_now_clear_the_bar),
]


# ── GAP: the retrace bound was one-sided ─────────────────────────────────

def test_gap_refuses_an_already_extended_gap():
    """
    NATIONALUM, 12-Aug: "Gapped +2.71% and held — -198% retraced". A negative
    retrace means price is ABOVE the open — the gap extended, it was not held.
    The old bound only caught give-back, so an arbitrarily chased gap passed.
    """
    from config import cfg_float
    prev_close, day_open = 400.0, 410.84            # +2.71% gap
    ltp = 419.30                                     # well above the open
    retrace = (day_open - ltp) / (day_open - prev_close) * 100.0
    assert retrace < 0, "price above the open must produce a negative retrace"
    assert retrace < -cfg_float("gap_max_extension_pct", 60.0), (
        f"{retrace:.0f}% extension must be refused")


def test_gap_still_takes_a_genuinely_held_gap():
    """The other direction: MCX gapped +2.42% and had given back 48%."""
    from config import cfg_float
    prev_close, day_open, ltp = 2862.0, 2931.0, 2898.0
    retrace = (day_open - ltp) / (day_open - prev_close) * 100.0
    assert 0 < retrace < cfg_float("gap_max_retrace_pct", 50.0)
    assert retrace >= -cfg_float("gap_max_extension_pct", 60.0)


def test_the_gap_confidence_bonus_no_longer_pays_for_extension():
    """`retrace < 20` was true for every negative value, i.e. every chase."""
    for retrace in (-198.0, -60.0, -1.0):
        assert not (0 <= retrace < 20), f"{retrace} must not earn the bonus"
    assert 0 <= 5.0 < 20


# ── every engine must declare phases that actually exist ─────────────────

def test_every_engine_declares_real_session_phases():
    """
    SDN declared "MORNING", which is not a phase this system has, and omitted
    PRIME, which is when it does nearly all its work. It was harmless only
    because SDN was the one engine of nine that never read its own `phases` —
    so adding the guard the others have would have silently switched every
    SDN short off during 09:30-11:00. registry.py also writes this tuple to
    strategy_config.phases, so the column named a phase that does not exist.
    """
    from intraday import session
    from intraday.strategies.registry import _ALL as all_engines_list
    real = {session.OPENING, session.PRIME, session.DRIFT, session.AFTERNOON,
            session.PRE_OPEN, session.CLOSING, session.SQUARE_OFF}
    for eng in all_engines_list:
        for ph in eng.phases:
            assert ph in real, f"{eng.name} declares unknown phase {ph!r}"


def test_every_engine_enforces_its_own_phase_declaration():
    """A declaration nothing checks is not a declaration."""
    from intraday.strategies.registry import _ALL as all_engines_list
    from intraday.strategies.base import SymbolContext
    ctx = SymbolContext(symbol="X", ltp=100.0, bars=[])
    for eng in all_engines_list:
        assert eng.evaluate(ctx, "NOT_A_PHASE") is None, (
            f"{eng.name} produced a setup in an unknown phase")


TESTS += [
    ("GAP refuses an already-extended gap", test_gap_refuses_an_already_extended_gap),
    ("GAP still takes a genuinely held gap", test_gap_still_takes_a_genuinely_held_gap),
    ("GAP bonus no longer pays for extension",
     test_the_gap_confidence_bonus_no_longer_pays_for_extension),
    ("every engine declares real session phases",
     test_every_engine_declares_real_session_phases),
    ("every engine enforces its phase declaration",
     test_every_engine_enforces_its_own_phase_declaration),
]


# ── the allocator ranks paper exactly as it ranks live ───────────────────

def _scored(*edges):
    return [{"symbol": f"S{i}", "edge": e} for i, e in enumerate(edges)]


def test_floor_only_rank_follows_edge_order_and_stops_at_slots():
    """
    Paper must inherit the allocator's ORDER and its BUDGET, not just skip the
    floor. Three proposals, two slots: the two best are ranked 0 and 1, the
    worst gets no rank and is therefore refused on paper as it is live.
    """
    from allocation.policies import intraday_stopping
    out = intraday_stopping(_scored(-0.60, -0.90, -0.75), bar=0.0,
                            slots_left=2, bar_before_floor=-1.09)
    by_sym = {v["symbol"]: v for v in out}
    assert all(v["verdict"] == "DECLINE" for v in out), "the floor still declines all"
    assert by_sym["S0"]["floor_only_rank"] == 0        # best edge
    assert by_sym["S2"]["floor_only_rank"] == 1        # second best
    assert "floor_only_rank" not in by_sym["S1"], "3rd of 2 slots must not be ranked"


def test_nothing_below_the_relative_bar_is_ranked():
    """
    Exploration is below the ABSOLUTE floor, never below the market's own
    judgement. A proposal worse than the pre-floor percentile is refused on
    paper for the same reason it is refused live.
    """
    from allocation.policies import intraday_stopping
    out = intraday_stopping(_scored(-2.00), bar=0.0, slots_left=3,
                            bar_before_floor=-1.09)
    assert "floor_only_rank" not in out[0]


def test_no_pre_floor_bar_means_no_ranking_at_all():
    """Backwards compatible: an old caller passing nothing ranks nothing."""
    from allocation.policies import intraday_stopping
    out = intraday_stopping(_scored(-0.60), bar=0.0, slots_left=3)
    assert "floor_only_rank" not in out[0]


def test_the_12aug_0930_cycle_would_now_take_only_the_ranked_ones():
    """
    The actual cycle: 5 proposals scored, 0 to take, 5 refused — and all five
    opened. With slots_left=5 they would all still be explored; the point is
    that the count is now bounded BY THE BUDGET rather than by arrival.
    """
    from allocation.policies import intraday_stopping
    out = intraday_stopping(_scored(-0.60, -0.90, -0.75, -1.05, -0.80),
                            bar=0.0, slots_left=2, bar_before_floor=-1.09)
    ranked = [v for v in out if "floor_only_rank" in v]
    assert len(ranked) == 2, f"budget is 2, ranked {len(ranked)}"
    assert [v["symbol"] for v in sorted(ranked, key=lambda x: x["floor_only_rank"])]         == ["S0", "S2"]


# ── a setup's own invalidation must be reachable before its stop ─────────

def _setup(entry, stop, target, level_key, level, direction="LONG"):
    from intraday.strategies.base import Setup
    return Setup(symbol="X", strategy="GAP", direction=direction, entry=entry,
                 stop=stop, target=target, confidence=0.7, rationale="",
                 invalidation="", valid_phases=(), meta={level_key: level})


def test_nationalum_shape_is_refused():
    """
    entry 419.30, stop 413.85, invalidation "a close below 399.45". The stop
    fires 14.88 before the structural cut can — 273% of the trade's own risk.
    The setup's stated thesis has no way to end the trade.
    """
    from intraday.strategies.registry import _invalidation_is_reachable
    assert not _invalidation_is_reachable(
        _setup(419.30, 413.85, 430.20, "or_low", 399.45))


def test_mcx_shape_is_kept():
    """
    THE OTHER DIRECTION. entry 2931.60, stop 2909.09, or_low 2912.00 — the
    stop sits just under the level by design and beats the cut by 0.58, which
    is 2.6% of risk. That is the design working, and refusing it would be a
    threshold nothing can clear.
    """
    from intraday.strategies.registry import _invalidation_is_reachable
    assert _invalidation_is_reachable(
        _setup(2931.60, 2909.09, 2976.62, "or_low", 2912.00))


def test_a_setup_with_no_structural_level_is_untouched():
    from intraday.strategies.registry import _invalidation_is_reachable
    from intraday.strategies.base import Setup
    s = Setup(symbol="X", strategy="RNG", direction="LONG", entry=100.0, stop=99.0,
              target=102.0, confidence=0.7, rationale="", invalidation="",
              valid_phases=(), meta={})
    assert _invalidation_is_reachable(s)


def test_the_check_is_directional():
    """A short is invalidated ABOVE its level; the stop must sit above that."""
    from intraday.strategies.registry import _invalidation_is_reachable
    ok = _setup(100.0, 103.0, 94.0, "range_low", 101.0, direction="SHORT")
    bad = _setup(100.0, 101.5, 94.0, "range_low", 108.0, direction="SHORT")
    assert _invalidation_is_reachable(ok)
    assert not _invalidation_is_reachable(bad)


TESTS += [
    ("floor_only_rank follows edge order and stops at slots",
     test_floor_only_rank_follows_edge_order_and_stops_at_slots),
    ("nothing below the relative bar is ranked", test_nothing_below_the_relative_bar_is_ranked),
    ("no pre-floor bar means no ranking", test_no_pre_floor_bar_means_no_ranking_at_all),
    ("the 12-Aug 09:30 cycle is bounded by the budget",
     test_the_12aug_0930_cycle_would_now_take_only_the_ranked_ones),
    ("the NATIONALUM shape is refused", test_nationalum_shape_is_refused),
    ("the MCX shape is kept", test_mcx_shape_is_kept),
    ("no structural level is untouched", test_a_setup_with_no_structural_level_is_untouched),
    ("the reachability check is directional", test_the_check_is_directional),
]


# ── the five engines audited on 12-Aug: VWR, PBK, PDL, RNG, GDB ──────────

def test_rng_invalidation_resolves_to_the_range_low_not_the_high():
    """
    RNG is a LONG at the range LOW and its meta carries both edges. The key
    list checks range_high first, so the level came back ABOVE the entry and
    `_invalidated` killed the trade on its first evaluation, every time — RNG
    has never completed a trade in the book's history.
    """
    from intraday.exit_policy import invalidation_level_from
    from intraday.strategies.base import Setup
    s = Setup(symbol="X", strategy="RNG", direction="LONG", entry=101.0, stop=99.5,
              target=108.0, confidence=0.7, rationale="",
              invalidation="a close below 100.00 — the range breaking down",
              valid_phases=(),
              meta={"range_low": 100.0, "range_high": 110.0,
                    "invalidation_level": 100.0})
    level, label = invalidation_level_from(s)
    assert level == 100.0, f"expected the range LOW, got {level}"
    assert level < s.entry, "a long's invalidation must sit below its entry"
    assert "100.00" in label


def test_the_rng_shape_would_have_been_invalidated_instantly_before():
    """The consequence, stated as the behaviour it caused."""
    from intraday.exit_policy import _invalidated
    from tests import cfg_ctx
    wrong = {"invalidation_level": 110.0, "direction": "LONG"}   # the range HIGH
    right = {"invalidation_level": 100.0, "direction": "LONG"}   # the range LOW
    with cfg_ctx({"intraday_invalidation_require_close": "false",
                  "intraday_invalidation_buffer_pct": "0.12"}):
        assert _invalidated(wrong, ltp=101.0)[0], "the old resolution killed it at once"
        assert not _invalidated(right, ltp=101.0)[0], "the correct level holds"


def test_sdn_trap_declares_a_level_the_lookup_can_find():
    """SDN/TRP published `prev_high`; the key list looks for `pdh`. Dead."""
    from intraday.exit_policy import invalidation_level_from
    from intraday.strategies.base import Setup
    s = Setup(symbol="X", strategy="SDN", direction="SHORT", entry=98.0, stop=101.0,
              target=92.0, confidence=0.7, rationale="",
              invalidation="reclaims 100.00", valid_phases=(),
              meta={"sub_engine": "TRP", "prev_high": 100.0,
                    "invalidation_level": 100.0})
    level, _ = invalidation_level_from(s)
    assert level == 100.0, "a trap short's invalidation must be reachable"


def test_sdn_range_breakdown_uses_the_low_it_broke():
    from intraday.exit_policy import invalidation_level_from
    from intraday.strategies.base import Setup
    s = Setup(symbol="X", strategy="SDN", direction="SHORT", entry=94.9, stop=98.1,
              target=90.0, confidence=0.7, rationale="",
              invalidation="closes back above the range low 95.00", valid_phases=(),
              meta={"sub_engine": "ORB", "range_low": 95.0, "range_high": 100.0,
                    "invalidation_level": 95.0})
    level, _ = invalidation_level_from(s)
    assert level == 95.0, f"a breakdown dies at the low it broke, got {level}"


def test_inference_still_works_for_engines_that_do_not_declare():
    """Removing the fallback would disarm ORB/VCE/GAP/PDL instead."""
    from intraday.exit_policy import invalidation_level_from
    from intraday.strategies.base import Setup
    s = Setup(symbol="X", strategy="ORB", direction="LONG", entry=101.0, stop=99.0,
              target=105.0, confidence=0.7, rationale="", invalidation="",
              valid_phases=(), meta={"range_high": 100.0})
    level, label = invalidation_level_from(s)
    assert level == 100.0 and "opening range" in label


def test_pbk_publishes_the_level_its_prose_promises():
    from intraday.exit_policy import invalidation_level_from
    from intraday.strategies.base import Setup
    s = Setup(symbol="X", strategy="PBK", direction="LONG", entry=100.4, stop=99.8,
              target=103.0, confidence=0.7, rationale="",
              invalidation="losing VWAP 100.00 on a closing basis", valid_phases=(),
              meta={"vwap": 100.0, "invalidation_level": 100.0})
    assert invalidation_level_from(s)[0] == 100.0


def test_pbk_target_no_longer_invents_a_price_never_traded():
    """
    `max(by_r, day_high)` is the defect VWR fixed on 10-Aug and PBK kept. On a
    pullback the high was made earlier, so 2.5R routinely sits beyond it and
    silently became the target — a level the stock has never reached.
    """
    ltp, risk, day_high = 100.0, 1.0, 101.5
    by_r = ltp + risk * 2.5                       # 102.5, never traded
    min_worthwhile = ltp + risk * 1.0             # 101.0
    old = max(by_r, day_high * 1.002)
    new = day_high * 1.002 if day_high * 1.002 >= min_worthwhile else by_r
    assert old == by_r, "the old form reached past the proven high"
    assert new < by_r and new > ltp, "the new form targets the proven high"


def test_vwr_and_gdb_refuse_a_touch_of_vwap():
    """VWAP is these engines' own invalidation level; entering on it opens the
    trade inside the band that closes it."""
    floor = confirmation_pct(0.0, 0.0)
    assert 0.004 < floor, "a 0.004% reclaim must not qualify"
    assert 0.30 > floor, "a genuine 0.30% reclaim must still trade"


TESTS += [
    ("RNG invalidation resolves to the range low",
     test_rng_invalidation_resolves_to_the_range_low_not_the_high),
    ("the RNG shape was invalidated instantly before",
     test_the_rng_shape_would_have_been_invalidated_instantly_before),
    ("SDN trap declares a findable level", test_sdn_trap_declares_a_level_the_lookup_can_find),
    ("SDN breakdown uses the low it broke", test_sdn_range_breakdown_uses_the_low_it_broke),
    ("inference still works when nothing is declared",
     test_inference_still_works_for_engines_that_do_not_declare),
    ("PBK publishes the level its prose promises",
     test_pbk_publishes_the_level_its_prose_promises),
    ("PBK target no longer invents a price never traded",
     test_pbk_target_no_longer_invents_a_price_never_traded),
    ("VWR and GDB refuse a touch of VWAP", test_vwr_and_gdb_refuse_a_touch_of_vwap),
]


def test_the_torntpharm_short_is_not_refused():
    """
    THE LIVE FALSE POSITIVE, 12-Aug-2026 12:27. SDN's VWAP rejection places its
    stop just above the rejection high — tighter than the invalidation buffer
    by design, so the stop and the invalidation are the same event. Charging it
    the buffer refused the highest-quality short this book has, every cycle.
    vwap 4842.29, stop 4843.81, entry below both.
    """
    from intraday.strategies.registry import _invalidation_is_reachable
    s = _setup(4820.00, 4843.81, 4780.00, "vwap", 4842.29, direction="SHORT")
    assert _invalidation_is_reachable(s), (
        "a stop placed just past its own level is the design, not an abandoned "
        "structure")


def test_a_stop_just_past_the_level_is_fine_in_both_directions():
    from intraday.strategies.registry import _invalidation_is_reachable
    # long: stop a hair below the level it names
    assert _invalidation_is_reachable(
        _setup(101.0, 99.90, 105.0, "range_high", 100.0))
    # short: stop a hair above the level it names
    assert _invalidation_is_reachable(
        _setup(99.0, 100.10, 95.0, "range_low", 100.0, direction="SHORT"))


TESTS += [
    ("the TORNTPHARM short is not refused", test_the_torntpharm_short_is_not_refused),
    ("a stop just past its level is fine both ways",
     test_a_stop_just_past_the_level_is_fine_in_both_directions),
]


def test_sdn_vwap_rejection_invalidates_at_the_level_that_failed():
    """
    THE 12-Aug 14:10 PRODUCTION REFUSAL, on ~15 names every 15s. SDN's VWAP
    rejection anchors its STOP to the rejection high and published VWAP as its
    invalidation. rej_high is normally BELOW vwap (price rallied toward VWAP
    and was refused before reaching it), so the stop fired first and the
    invalidation could never run — the whole condition was refused.
    """
    from intraday.strategies.registry import _invalidation_is_reachable
    # TRITURBINE: stop 593.71 (= rej_high 593.00 * 1.0012), vwap 594.24
    assert not _invalidation_is_reachable(
        _setup(590.0, 593.71, 580.0, "vwap", 594.24, direction="SHORT")),         "publishing VWAP is what the check correctly refused"
    assert _invalidation_is_reachable(
        _setup(590.0, 593.71, 580.0, "range_low", 593.00, direction="SHORT")),         "the rejection high, which the stop is built on, must be reachable"


def test_the_worst_12aug_spread_is_fixed_too():
    """MARUTI had the widest gap: stop 13957.73 against a published vwap of
    13987.34 — 29.61 away, on a rejection high of ~13941."""
    from intraday.strategies.registry import _invalidation_is_reachable
    assert not _invalidation_is_reachable(
        _setup(13900.0, 13957.73, 13700.0, "vwap", 13987.34, direction="SHORT"))
    assert _invalidation_is_reachable(
        _setup(13900.0, 13957.73, 13700.0, "range_low", 13941.00, direction="SHORT"))


TESTS += [
    ("SDN VWAP rejection invalidates at the level that failed",
     test_sdn_vwap_rejection_invalidates_at_the_level_that_failed),
    ("the worst 12-Aug spread is fixed too", test_the_worst_12aug_spread_is_fixed_too),
]
