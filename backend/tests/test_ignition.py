"""
IGN (Ignition Momentum) — new engine, 08-Sep-2026. See
intraday/strategies/ignition.py's own module docstring for the full
quantification and reasoning; this pins the mechanics and the ACTIVE launch
(the inverse of GDB's own SHADOW-launch test file — IGN ships capital-
eligible from day one, the operator's own explicit instruction).

FIXTURE SHAPE, READ BEFORE CHANGING A NUMBER
----------------------------------------------
Unlike every other engine's own test fixtures, the "ignition" here is
represented as a GAP BETWEEN `ltp` and the bar history, not as a ramp built
entirely out of bars: `bars` are the completed history (mostly flat, near
`prev_close`), and `ltp` is the current, still-forming tick already well
past them — exactly how a live feed looks the moment a move ignites. Most
tests additionally override `ign_stop_lookback_bars` down to 3, so the
recent-window stop sits close to `ltp` (a tight, already-caught-up
consolidation) rather than back at the pre-move base — the DEFAULT lookback
(20) is deliberately exercised by exactly one test
(`test_default_lookback_produces_a_stop_too_wide_to_take`), which is what
proves risk_from_structure()'s refuse-don't-reprice contract actually bites
on a still-extended base, not just on paper.
"""
from __future__ import annotations

from config import cfg_float
from intraday.session import OPENING, PRIME, DRIFT, AFTERNOON, CLOSING
from intraday.strategies.base import SymbolContext
from tests import cfg_ctx
from tests._fixtures import OPEN, bars as _bars


def _long_ctx(volume=80_000, avg_volume_20d=3_000_000, upper_circuit=None,
             lower_circuit=None, atr_pct_daily=2.0):
    """10 bars ramping 100.5 -> 105.7 (tight over the last 3), ltp=106.0,
    prev_close=100.0 -> chg_pct=+6.0%. With the default ign_stop_lookback_
    bars (20) the window covers the whole ramp and the stop is too wide —
    see the module docstring above. Tests that need a takeable setup
    override the lookback to 3 via cfg_ctx."""
    closes = [100.5, 101.5, 102.5, 103.5, 104.3, 105.0, 105.3, 105.5, 105.6, 105.7]
    bs = _bars([(c - 0.2, c + 0.2, c - 0.5, c, volume) for c in closes])
    return SymbolContext(
        symbol="IGNCO", ltp=106.0, bars=bs, vwap=104.0,
        day_open=closes[0], day_high=max(b.high for b in bs),
        day_low=min(b.low for b in bs),
        prev_close=100.0, prev_high=100.5, prev_low=99.0,
        atr_pct_daily=atr_pct_daily, avg_volume_20d=avg_volume_20d,
        value_cr=150.0, sector="IT", rs_vs_index_pct=1.0, as_of=OPEN,
        upper_circuit=upper_circuit, lower_circuit=lower_circuit,
    )


def _short_ctx(volume=80_000, avg_volume_20d=3_000_000, upper_circuit=None,
              lower_circuit=None, ltp=95.5, prev_close=100.0):
    """Mirror of _long_ctx: 10 bars ramping 99.0 -> 95.5 (tight over the
    last 3), ltp defaults to 95.5 (chg_pct -4.5%) — inside can_short()'s
    own -6.0% 'already collapsed' floor, so the light self-check passes.
    A separate test (test_short_refused_by_can_short_already_collapsed)
    pushes ltp past that floor deliberately."""
    closes = [99.0, 98.0, 97.0, 96.3, 95.9, 95.7, 95.6, 95.55, 95.52, 95.5]
    bs = _bars([(c + 0.2, c + 0.5, c - 0.2, c, volume) for c in closes])
    return SymbolContext(
        symbol="IGNCO", ltp=ltp, bars=bs, vwap=97.0,
        day_open=closes[0], day_high=max(b.high for b in bs),
        day_low=min(b.low for b in bs),
        prev_close=prev_close, prev_high=100.5, prev_low=94.0,
        atr_pct_daily=2.0, avg_volume_20d=avg_volume_20d,
        value_cr=150.0, sector="IT", rs_vs_index_pct=-1.0, as_of=OPEN,
        upper_circuit=upper_circuit, lower_circuit=lower_circuit,
    )


_TIGHT_LOOKBACK = {"ign_stop_lookback_bars": "3"}


# ── the happy paths ─────────────────────────────────────────────────────────

def test_fires_long_on_big_move_and_volume_with_no_circuit_data():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx()  # upper_circuit/lower_circuit left at their SymbolContext default (None)
    assert ctx.upper_circuit is None and ctx.lower_circuit is None, (
        "fixture must exercise the graceful-degradation path, not incidentally avoid it")
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is not None, "a genuine ignition move with no circuit data must still fire"
    assert s.direction == "LONG"
    ok, why = s.coherent()
    assert ok, f"engine emitted incoherent levels — {why}"


def test_fires_short_on_collapse_and_volume_with_no_circuit_data():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _short_ctx()
    assert ctx.upper_circuit is None and ctx.lower_circuit is None
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is not None, "a genuine collapse with no circuit data must still fire"
    assert s.direction == "SHORT"
    ok, why = s.coherent()
    assert ok, f"engine emitted incoherent levels — {why}"


# ── circuit-frozen refusal ───────────────────────────────────────────────────

def test_refuses_long_already_frozen_at_upper_circuit():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx(upper_circuit=106.05)   # 0.047% off ltp — within the 0.10% tolerance
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "nothing to buy into at a frozen upper circuit"


def test_refuses_short_already_frozen_at_lower_circuit():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _short_ctx(lower_circuit=95.45)   # 0.052% off ltp — within tolerance
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "no bid to sell into at a frozen lower circuit"


def test_circuit_proximity_raises_confidence_but_never_gates_a_normal_entry():
    """The operator's own explicit requirement: distance-to-circuit informs
    confidence only. A LONG comfortably clear of (not frozen at) the upper
    circuit must fire, and score higher confidence than an identical setup
    with no circuit data at all."""
    from intraday.strategies.ignition import IgnitionMomentum
    near = _long_ctx(upper_circuit=107.5)     # ~1.4% away — within ign_circuit_near_pct (2.0)
    far = _long_ctx()                         # no circuit data
    with cfg_ctx(_TIGHT_LOOKBACK):
        s_near = IgnitionMomentum().evaluate(near, PRIME)
        s_far = IgnitionMomentum().evaluate(far, PRIME)
    assert s_near is not None and s_far is not None
    assert s_near.confidence > s_far.confidence, (
        "proximity to an unfrozen circuit must raise confidence, not gate entry")


# ── shortability self-gate ───────────────────────────────────────────────────

def test_short_refused_by_can_short_already_collapsed():
    """Chosen over trying to trigger can_short()'s upper-circuit proxy,
    which CANNOT bind on IGN's own SHORT leg — that leg already requires
    chg_pct deeply negative, structurally incompatible with also reading as
    sharply up (see ignition.py's own module docstring). -6.5% clears
    IGN's own -3.5% trigger easily but trips can_short()'s -6.0% 'already
    collapsed' floor — the light self-check must still refuse it."""
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _short_ctx(ltp=93.5, prev_close=100.0)   # -6.5%
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "can_short()'s already-collapsed floor must refuse this short"


# ── volume: required, not merely rewarded ────────────────────────────────────

def test_volume_solid_but_not_violent_is_refused():
    """volume_ratio ~1.53x — solidly above GAP's own 1.30 floor, comfortably
    below IGN's 2.00 — must still be refused. Distinguishes an ordinary
    breakout from the violent signature this engine exists to catch."""
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx(volume=11_000)
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "solid-but-not-violent volume must not qualify"


def test_missing_volume_data_is_refused_not_passed():
    """Pins the deliberate GAP/VCE/GDB-divergent choice: those three treat
    a None volume_ratio as 'pass, unconfirmed'. IGN has no structural break
    to lean on, so a missing reading is a refusal, not a shrug."""
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx(avg_volume_20d=None)
    with cfg_ctx(_TIGHT_LOOKBACK):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "missing volume data must refuse, not pass unconfirmed"


# ── stop: a level, refused not re-priced when too wide ──────────────────────

def test_default_lookback_produces_a_stop_too_wide_to_take():
    """No lookback override — the window covers the WHOLE ramp back to the
    pre-move base, so the structural stop is far below ltp. Proves
    risk_from_structure()'s refuse-don't-reprice contract actually bites
    here, not just in theory."""
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx()
    with cfg_ctx({}):   # default ign_stop_lookback_bars (20)
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "a stop back at the pre-move base must be refused, not tightened"


# ── magnitude ─────────────────────────────────────────────────────────────

def test_magnitude_below_threshold_is_refused():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx()
    with cfg_ctx({**_TIGHT_LOOKBACK, "ign_min_pct": "10.0", "ign_min_atr_frac": "10.0"}):
        s = IgnitionMomentum().evaluate(ctx, PRIME)
    assert s is None, "a +6.0% move must not qualify against a 10% floor"


# ── phases ────────────────────────────────────────────────────────────────

def test_refuses_outside_declared_phases():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx()
    with cfg_ctx(_TIGHT_LOOKBACK):
        for phase in (OPENING, CLOSING):
            assert IgnitionMomentum().evaluate(ctx, phase) is None, (
                f"must not fire in {phase} — the main loop cannot reach it either")


def test_fires_in_drift_and_afternoon_too():
    from intraday.strategies.ignition import IgnitionMomentum
    ctx = _long_ctx()
    with cfg_ctx(_TIGHT_LOOKBACK):
        for phase in (PRIME, DRIFT, AFTERNOON):
            assert IgnitionMomentum().evaluate(ctx, phase) is not None, (
                f"must fire in {phase} — unlike ORB/GAP, IGN is not opening-only")


# ── wiring: registered, and launched ACTIVE — not the SHADOW default other
#    new engines have used, the operator's own explicit choice ─────────────

def test_registered_in_the_engine_list():
    from intraday.strategies.registry import engine_names
    assert "IGN" in engine_names()


def test_unconfigured_lifecycle_already_defaults_active_matching_the_shipped_row():
    """Unlike GDB's own equivalent test — where the unconfigured default
    (ACTIVE) was exactly the danger the SHADOW row exists to override —
    IGN's migration 132 row (ACTIVE) matches what registry.py would already
    default to. The row is still shipped explicitly (auditable, not
    incidental), but this test documents that the two are consistent, not
    that the row is overriding a dangerous default."""
    from intraday.strategies.registry import engine_lifecycle, LIFECYCLE_ACTIVE
    with cfg_ctx({}):   # no intraday_engine_ign_lifecycle key at all
        assert engine_lifecycle("IGN") == LIFECYCLE_ACTIVE


def test_configured_active_is_honoured():
    from intraday.strategies.registry import engine_lifecycle, LIFECYCLE_ACTIVE
    with cfg_ctx({"intraday_engine_ign_lifecycle": "ACTIVE"}):
        assert engine_lifecycle("IGN") == LIFECYCLE_ACTIVE


def test_active_ign_setup_can_win_best():
    """The inverse of GDB's own SHADOW-can't-win test: with IGN ACTIVE and
    every other engine disabled, its own setup must be capital-eligible."""
    from intraday.strategies.registry import evaluate_all
    ctx = _long_ctx()
    with cfg_ctx({**_TIGHT_LOOKBACK,
                  "intraday_engine_ign_lifecycle": "ACTIVE",
                  "intraday_engine_orb_enabled": "false",
                  "intraday_engine_gap_enabled": "false",
                  "intraday_engine_pdl_enabled": "false",
                  "intraday_engine_vce_enabled": "false",
                  "intraday_engine_pbk_enabled": "false",
                  "intraday_engine_vwr_enabled": "false",
                  "intraday_engine_rng_enabled": "false",
                  "intraday_engine_sdn_enabled": "false",
                  "intraday_engine_gdb_enabled": "false"}):
        best, found = evaluate_all(ctx, PRIME)
    assert any(s.strategy == "IGN" for s in found), "IGN must evaluate and record"
    assert best is not None and best.strategy == "IGN", (
        "an ACTIVE IGN setup must be capital-eligible, unlike a SHADOW one")


TESTS = [
    ("fires LONG on big move + volume with no circuit data",
     test_fires_long_on_big_move_and_volume_with_no_circuit_data),
    ("fires SHORT on collapse + volume with no circuit data",
     test_fires_short_on_collapse_and_volume_with_no_circuit_data),
    ("refuses LONG already frozen at the upper circuit",
     test_refuses_long_already_frozen_at_upper_circuit),
    ("refuses SHORT already frozen at the lower circuit",
     test_refuses_short_already_frozen_at_lower_circuit),
    ("circuit proximity raises confidence but never gates a normal entry",
     test_circuit_proximity_raises_confidence_but_never_gates_a_normal_entry),
    ("SHORT refused by can_short()'s already-collapsed floor",
     test_short_refused_by_can_short_already_collapsed),
    ("volume solid but not violent is refused",
     test_volume_solid_but_not_violent_is_refused),
    ("missing volume data is refused, not passed",
     test_missing_volume_data_is_refused_not_passed),
    ("default lookback produces a stop too wide to take",
     test_default_lookback_produces_a_stop_too_wide_to_take),
    ("magnitude below threshold is refused",
     test_magnitude_below_threshold_is_refused),
    ("refuses outside declared phases",
     test_refuses_outside_declared_phases),
    ("fires in DRIFT and AFTERNOON too",
     test_fires_in_drift_and_afternoon_too),
    ("registered in the engine list",
     test_registered_in_the_engine_list),
    ("unconfigured lifecycle already defaults ACTIVE, matching the shipped row",
     test_unconfigured_lifecycle_already_defaults_active_matching_the_shipped_row),
    ("configured ACTIVE is honoured",
     test_configured_active_is_honoured),
    ("an ACTIVE IGN setup can win best",
     test_active_ign_setup_can_win_best),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
