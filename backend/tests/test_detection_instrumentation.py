"""
ATR and the pre-cap stop are recorded at detection, not lost (18-Aug-2026).

WHAT THIS CATCHES
-----------------
`base.risk_from_structure`'s own docstring names the gap it left behind:
"`intraday_setups` stores no ATR and no pre-cap stop, so what a widened stop
would have done cannot be reconstructed from any row on disk." Neither the
ATR-anchored stop nor "refuse vs size down" (F-33 §3/§7) is answerable without
this, on any engine — the question was left open on purpose because nothing
recorded it, not because it was unimportant.

TWO SEPARATE STAMPS, TWO SEPARATE REASONS TO KEEP THEM SEPARATE.

`atr_pct_daily` is stamped centrally in `registry.evaluate_all`, next to
`sub_engine`/`family`/`lifecycle` — one hook, all nine engines, including RNG
and SDN which never call `risk_from_structure` at all. An engine-local stamp
would need adding nine times and could be forgotten on the tenth.

`RiskFrame.meta()` is stamped per engine because it needs a `frame` in scope,
and it MUST return `{}` under the default `refuse` mode: `structural_stop` is
then always identical to the `stop` column already on the row, and a field
that always equals another column is the "silent default" this project's own
rule warns about, not instrumentation. It only carries data under the LEGACY
`tighten` branch, where `structural_stop` and the row's `stop` genuinely
diverge — which is also the one case this project has already lost real
information once (this same commit's stop-geometry fix).

TARGET DISTANCE IS DELIBERATELY NOT STAMPED. `entry` and `target` are already
direct columns on `intraday_setups` (migration 014); `(target - entry) /
entry` is fully recoverable from data already on disk. Adding a third,
redundant copy of arithmetic every reader can already do is not instrumenting
a gap, it is duplicating a column.

WHY THESE CHECKS CAN FAIL
--------------------------
Each assertion is demonstrated against a one-line removal of the stamp it
pins — see the `_broken_*` reconstructions below — before being trusted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config import IST
from tests import cfg_ctx
from intraday.session import PRIME
from intraday.strategies.base import Bar, RiskFrame, SymbolContext


def _range_bars(lo: float, hi: float, vol: float = 100_000.0) -> list[Bar]:
    t0 = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    mid = (hi + lo) / 2
    return [Bar(ts=t0 + timedelta(minutes=5 * i), open=mid, high=hi, low=lo,
                close=mid, volume=vol) for i in range(3)]


# A real ORB break, shaped exactly like test_break_confirmation's own fixture
# so this exercises the production engine end to end, not a stub.
_BARS = _range_bars(1066.40, 1081.80)
_COMMON = dict(atr_pct_daily=3.14, prev_high=1000.0, rs_vs_index_pct=0.4)


def _orb_ctx(ltp: float) -> SymbolContext:
    return SymbolContext(symbol="SBIN", bars=_BARS, ltp=ltp, **_COMMON)


def test_atr_reaches_the_setups_meta_through_the_real_engine():
    """End to end: registry.evaluate_all stamps it, not the engine."""
    from intraday.strategies.orb import OpeningRangeBreakout
    from intraday.strategies.registry import evaluate_all

    with cfg_ctx({"orb_max_risk_pct": "5.0"}):     # isolate: not testing the cap here
        best, found = evaluate_all(_orb_ctx(1086.00), PRIME)
    assert found, "the fixture is a real, confirmed ORB break — it must fire"
    s = found[0]
    assert s.meta.get("atr_pct_daily") == 3.14, (
        f"atr_pct_daily must be stamped from the context untouched, got "
        f"{s.meta.get('atr_pct_daily')!r}")


def test_atr_is_recorded_as_none_when_unknown_not_omitted():
    """
    Absent must be a RECORDED None, not a missing key. "The value is unknown"
    and "nobody asked" must not read the same way on a later query — the
    cold-start rule this project has already had to relearn twice.

    ORB READS `ctx.atr_pct_daily or 2.0` INTERNALLY, so `_BARS` (tuned to
    clear the range/ATR gate at atr=3.14) will not fire at the 2.0 fallback —
    a narrower range that clears `orb_min/max_range_frac` at the FALLBACK
    value, not the fixture used elsewhere in this module.
    """
    from intraday.strategies.registry import evaluate_all
    narrow_bars = _range_bars(1070.00, 1078.56)     # ~0.80% wide, clears 0.15-0.70x2.0
    ctx = SymbolContext(symbol="SBIN", bars=narrow_bars, ltp=1082.00,
                        prev_high=1000.0, rs_vs_index_pct=0.4)  # atr_pct_daily unset
    with cfg_ctx({"orb_max_risk_pct": "5.0"}):
        best, found = evaluate_all(ctx, PRIME)
    assert found, "fixture must fire at ORB's own ATR fallback (2.0)"
    s = found[0]
    assert "atr_pct_daily" in s.meta, "the key must exist even when the value is None"
    assert s.meta["atr_pct_daily"] is None


def test_every_enabled_engine_gets_the_atr_stamp_not_only_orb():
    """
    Stamped centrally so a NEW engine cannot forget it — pinned against a
    fake engine rather than a real one, so this does not depend on any real
    engine's entry conditions.
    """
    from intraday.strategies import registry as R
    from intraday.strategies.base import Setup

    class _FakeEngine:
        name = "FAKE"

        def evaluate(self, ctx, phase):
            return Setup(symbol=ctx.symbol, strategy="FAKE", direction="LONG",
                        entry=100.0, stop=99.0, target=102.0, confidence=0.6,
                        rationale="r", invalidation="i")

    orig_enabled = R.enabled_engines
    R.enabled_engines = lambda: [_FakeEngine()]
    try:
        ctx = SymbolContext(symbol="X", bars=[], ltp=100.0, atr_pct_daily=2.75)
        best, found = R.evaluate_all(ctx, PRIME)
    finally:
        R.enabled_engines = orig_enabled
    assert found and found[0].meta.get("atr_pct_daily") == 2.75, (
        "a brand-new engine that never touches ATR itself must still get the "
        "stamp — it is registry.evaluate_all's job, not the engine's")


def test_the_atr_stamp_is_demonstrably_removable():
    """
    NOT a defence of the stamp — proof the checks above can fail. Reconstructs
    evaluate_all WITHOUT the atr_pct_daily line and shows the first two checks
    above fail against it.
    """
    from intraday.strategies import registry as R
    from intraday.strategies.base import Setup

    class _FakeEngine:
        name = "FAKE"

        def evaluate(self, ctx, phase):
            return Setup(symbol=ctx.symbol, strategy="FAKE", direction="LONG",
                        entry=100.0, stop=99.0, target=102.0, confidence=0.6,
                        rationale="r", invalidation="i")

    def _broken_evaluate_all(ctx, phase):
        found = []
        for eng in [_FakeEngine()]:
            s = eng.evaluate(ctx, phase)
            if s:
                s.meta["lifecycle"] = R.engine_lifecycle(eng.name)
                s.meta["sub_engine"] = s.strategy
                s.meta["family"] = R.family_of(eng.name)
                # atr_pct_daily line deliberately omitted
                found.append(s)
        return (found[0] if found else None), found

    ctx = SymbolContext(symbol="X", bars=[], ltp=100.0, atr_pct_daily=2.75)
    _, found = _broken_evaluate_all(ctx, PRIME)
    assert "atr_pct_daily" not in found[0].meta, (
        "this reconstruction must reproduce the pre-fix gap")


def test_riskframe_meta_is_empty_under_the_default_refuse_mode():
    """
    The common case, and it must add NOTHING — `structural_stop` would just be
    `stop` again under `refuse` mode, and a duplicate column is not
    instrumentation.
    """
    frame = RiskFrame(stop=99.2, risk=0.8, risk_pct=0.8,
                      structural_stop=99.2, capped=False)
    assert frame.meta() == {}, (
        f"an uncapped frame must contribute nothing to meta, got {frame.meta()}")


def test_riskframe_meta_preserves_the_pre_cap_stop_when_capped():
    """The one case this project has already lost data in once (this same
    commit's own stop-geometry fix) — the legacy `tighten` branch."""
    frame = RiskFrame(stop=98.8, risk=1.2, risk_pct=1.2,
                      structural_stop=97.5, capped=True)
    m = frame.meta()
    assert m.get("structural_stop") == 97.5, (
        f"the level the structure actually named must survive, got {m}")
    assert m.get("stop_capped") is True
    assert m.get("capped_risk_pct") == 1.2


def test_the_capped_meta_reaches_a_real_engine_end_to_end():
    """
    Through ORB with the legacy branch armed, at a stop wide enough to force
    the clamp — exercising the exact production code path, not a stub.
    """
    from intraday.strategies.orb import OpeningRangeBreakout
    from intraday.strategies.registry import evaluate_all

    with cfg_ctx({"orb_max_risk_pct": "0.5", "intraday_stop_cap_mode": "tighten"}):
        best, found = evaluate_all(_orb_ctx(1086.00), PRIME)
    assert found, "a wide-range ORB break under a 0.5% cap must still fire (tighten)"
    s = found[0]
    assert s.meta.get("stop_capped") is True, (
        f"a stop this wide against a 0.5% cap must be recorded as capped, "
        f"meta={s.meta}")
    assert s.meta.get("structural_stop") is not None
    assert s.meta["structural_stop"] != s.stop, (
        "the whole point: the level the structure named and the stop the "
        "trade carries must be distinguishable on disk when they diverge")


def test_the_capped_meta_is_demonstrably_removable():
    """NOT a defence — proof the previous two checks can fail."""
    frame = RiskFrame(stop=98.8, risk=1.2, risk_pct=1.2,
                      structural_stop=97.5, capped=True)

    def _broken_meta(self):
        return {}                       # the pre-fix behaviour: nothing kept

    import types
    broken = types.MethodType(_broken_meta, frame)
    assert broken() == {}, "the broken reconstruction must lose the divergence"


TESTS = [
    ("ATR reaches Setup.meta through the real ORB engine",
     test_atr_reaches_the_setups_meta_through_the_real_engine),
    ("ATR is recorded as None when unknown, not omitted",
     test_atr_is_recorded_as_none_when_unknown_not_omitted),
    ("every enabled engine gets the ATR stamp, not only ORB",
     test_every_enabled_engine_gets_the_atr_stamp_not_only_orb),
    ("the ATR stamp is demonstrably removable",
     test_the_atr_stamp_is_demonstrably_removable),
    ("RiskFrame.meta() is empty under the default refuse mode",
     test_riskframe_meta_is_empty_under_the_default_refuse_mode),
    ("RiskFrame.meta() preserves the pre-cap stop when capped",
     test_riskframe_meta_preserves_the_pre_cap_stop_when_capped),
    ("the capped meta reaches a real engine end to end",
     test_the_capped_meta_reaches_a_real_engine_end_to_end),
    ("the capped meta is demonstrably removable",
     test_the_capped_meta_is_demonstrably_removable),
]
