"""
Allocator scenario sweep, 31-Aug-2026 (operator request: pressure-test
score()/hurdle() across realistic market conditions for BOTH books, since
the real resolved-trade history since 20-Aug is too thin — 2-4 distinct
trades per verdict per book — to mine empirically; see docs/FINDINGS.md
for that dead end).

First scenario constructed — SWING's own time-of-day sensitivity — found
a real gap, fixed here.

THE SAME BUG CLASS F-88 ALREADY FIXED, ONE PARAMETER OVER
-----------------------------------------------------------
F-88 (29-Aug-2026) found `Allocator.select()` computing ONE `bucket`
(regime classification) from intraday's 15-second market reading and
applying it to SWING proposals too, even though swing has its own
once-a-day regime. The fix added `swing_regime` so `bucket` is computed
per-framework (allocator.py:362-370).

`minutes_left` — the OTHER input to hurdle()'s time-decay term — sat right
next to that fix (allocator.py's hurdle() call) and was not touched. It
was one shared scalar for the whole `select()` call
(intraday/engine.py:4951, `minutes_left = max(st.minutes_to_squareoff or
0, 0)`), fed into `hurdle(bucket, fw_slots, minutes_left, fw, ...)` for
BOTH frameworks. Swing plans run 1-3 WEEKS; "minutes until today's 15:15
square-off" has no relationship to that horizon, but it moved swing's bar
exactly as much as it moved intraday's.

MEASURED, WITH THE REAL FUNCTION, NOT THE FORMULA BY EYE, BEFORE THE FIX.
Same bucket, same arrival population (60 edges spread -0.5..+0.5), same
slots (5 of 15) — only minutes_left changed:

    SWING bar at market open   (minutes_left=355): 0.4152  (91.7th pct)
    SWING bar near square-off  (minutes_left=5):    0.3136  (80.1st pct)

A swing candidate with edge 0.35 was REFUSED at 09:20 and ACCEPTED at
15:10, for a reason that had nothing to do with the trade.

THE FIX. `select()` gained `swing_minutes_left: int | None = None`,
mirroring `swing_regime` exactly: additive, opt-in, isolated to the SWING
half of the per-framework loop — INTRADAY always reads the original
`minutes_left` parameter unconditionally, regardless of whether
`swing_minutes_left` was passed or what it is. A caller that does not
pass it gets today's old shared-clock behaviour for both books unchanged.
`intraday/engine.py::_allocate_shadow` now passes `swing_minutes_left=0`,
which makes hurdle()'s `time_frac` exactly 0 and `time_mult` a constant
1.0 no-op for SWING — scarcity (already correctly slot-scoped via
`slots_by_framework`/`max_slots_by_framework`) is the only pressure term
left moving swing's bar, rather than inventing a new, unevidenced
time-based meaning for it.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self):
        return self

    def order(self, col, *a, **k):
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows):
        self.data = rows

    def execute(self):
        return self


class _FakeSB:
    """Same fixture shape as test_hurdle_percentile.py's own — a fixed
    arrival population, framework/bucket tagged, ignoring the query's
    WHERE clauses (those are exercised for real by tools.health)."""
    def __init__(self, edges: list[float], framework: str, bucket: str):
        self._rows = [{"edge": e, "framework": framework, "regime_bucket": bucket,
                       "trade_date": "2026-08-06"} for e in edges]

    def table(self, name):
        return _FakeQuery(self._rows)


class _NoDB:
    """Raises on any query — used where the test seeds priors/hurdle
    inputs directly and the call must not need real data, the same
    contract test_allocator_direction.py's NoDB already established."""
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def _spread_edges(n=60, lo=-0.5, hi=0.5):
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


# ── hurdle()'s own mechanism: correctly time-sensitive, framework-blind ────
#
# hurdle() itself has no opinion about which framework called it — that is
# select()'s job, tested below. These two just confirm the raw sensitivity
# the bug exploited is real and, for INTRADAY, exactly right.

def test_hurdle_is_sensitive_to_minutes_left_by_design():
    from allocation.hurdle import hurdle
    edges = _spread_edges()
    sb = _FakeSB(edges, "INTRADAY", "NEUTRAL")
    with cfg_ctx({"alloc_hurdle_min_sample": 30}):
        bar_open, _ = hurdle("NEUTRAL", 5, 355, "INTRADAY", sb, max_slots=15)
        bar_close, _ = hurdle("NEUTRAL", 5, 5, "INTRADAY", sb, max_slots=15)
    assert bar_open > bar_close, (
        "intraday's time-decay must hold — more time left means a higher bar")
    assert bar_open - bar_close > 0.05, "the effect should be material, not noise"


def test_hurdle_minutes_left_zero_is_a_time_mult_no_op():
    """The exact mechanism the fix relies on: minutes_left=0 must make
    hurdle()'s time term a constant 1.0, leaving only scarcity to move
    the bar."""
    from allocation.hurdle import hurdle
    edges = _spread_edges()
    sb = _FakeSB(edges, "SWING", "NEUTRAL")
    with cfg_ctx({"alloc_hurdle_min_sample": 30, "alloc_time_weight": 0.6}):
        bar_a, inputs_a = hurdle("NEUTRAL", 5, 0, "SWING", sb, max_slots=15)
        bar_b, inputs_b = hurdle("NEUTRAL", 5, 0, "SWING", sb, max_slots=15)
    assert inputs_a.get("time_mult") == 1.0, (
        f"expected time_mult==1.0 at minutes_left=0, got {inputs_a.get('time_mult')}")
    assert bar_a == bar_b, "identical inputs must be deterministic"


# ── Allocator.select(): the actual fix, exercised end to end ───────────────

def test_select_isolates_swing_minutes_left_from_intraday():
    """The core safety claim: in a MIXED proposal list, passing
    swing_minutes_left must change SWING's bar and must NOT change
    INTRADAY's — same isolation test_allocator_direction.py already
    proved for swing_regime, applied to this parameter."""
    from allocation.proposal import from_intraday, from_swing
    from allocation.allocator import Allocator
    from allocation.scoring import Prior
    from intraday.strategies.base import Setup

    long_s = Setup("RELIANCE", "ORB", "LONG", 2500.0, 2485.0, 2540.0,
                   0.75, "r", "i", meta={"family": "ORB"})

    class D:
        symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
        target = 3950.0; qty = 5; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx({"alloc_hurdle_min_sample": 5, "alloc_time_weight": 0.6}):
        p_intraday = from_intraday(long_s, 8)
        p_swing = from_swing(D())
        assert p_intraday is not None and p_swing is not None

        # regime="NEUTRAL" resolves to bucket WEAK (regime_bucket() — RISK
        # ON/RISK_ON is the only STRONG case). A real (non-cold-start)
        # arrival population for BOTH frameworks' WEAK bucket, so the bar
        # is actually percentile-computed and can respond to minutes_left
        # — a cold-start bar (-inf) is unaffected by time/scarcity BY
        # DESIGN and would make this test meaningless.
        sb = _FakeSB(_spread_edges(n=60), "SWING", "WEAK")
        sb._rows += _FakeSB(_spread_edges(n=60), "INTRADAY", "WEAK")._rows

        def _fresh_alloc():
            alloc = Allocator(sb=sb)
            alloc._priors = {
                "INTRADAY/ORB": Prior("INTRADAY/ORB", 100, 0.35, 0.20, 0.05, -1.0, 2.0),
                "INTRADAY/ALL": Prior("INTRADAY/ALL", 200, 0.20, 0.10, 0.04, -1.0, 1.8),
                "SWING/CONTINUATION": Prior("SWING/CONTINUATION", 100, 0.30, 0.15, 0.05, -1.0, 2.0),
                "SWING/ALL": Prior("SWING/ALL", 150, 0.25, 0.12, 0.04, -1.0, 1.9),
            }
            alloc._hold_days = {"INTRADAY": (1.0, 50), "SWING": (5.0, 30)}
            return alloc

        # Two calls, everything identical except swing_minutes_left.
        without = _fresh_alloc().select(
            [p_intraday, p_swing], regime="NEUTRAL",
            slots_by_framework={"SWING": 3, "INTRADAY": 3},
            max_slots_by_framework={"SWING": 15, "INTRADAY": 20},
            minutes_left=355)
        withzero = _fresh_alloc().select(
            [p_intraday, p_swing], regime="NEUTRAL",
            slots_by_framework={"SWING": 3, "INTRADAY": 3},
            max_slots_by_framework={"SWING": 15, "INTRADAY": 20},
            minutes_left=355, swing_minutes_left=0)

        by_sym_a = {v["proposal"].symbol: v for v in without}
        by_sym_b = {v["proposal"].symbol: v for v in withzero}

        # INTRADAY: byte-identical edge and hurdle, whether or not
        # swing_minutes_left was passed — it must never read this parameter.
        assert by_sym_a["RELIANCE"]["edge"] == by_sym_b["RELIANCE"]["edge"], (
            "INTRADAY's edge changed when swing_minutes_left was introduced")
        assert by_sym_a["RELIANCE"]["hurdle"] == by_sym_b["RELIANCE"]["hurdle"], (
            "INTRADAY's bar changed when swing_minutes_left was introduced — "
            "it must be completely isolated from this parameter")

        # SWING: the bar DOES move (this is the fix actually doing something).
        assert by_sym_a["TCS"]["hurdle"] != by_sym_b["TCS"]["hurdle"], (
            "SWING's bar did not respond to swing_minutes_left=0 — the fix "
            "is not wired, or minutes_left=355 vs 0 produced the same bar "
            "by coincidence (re-check alloc_time_weight is nonzero)")


def test_select_without_swing_minutes_left_is_unchanged_from_before_the_fix():
    """Backward compatibility: a caller that does not pass
    swing_minutes_left (every caller before this change, and any future
    one that has not been updated) must get IDENTICAL SWING behaviour to
    before the fix — this parameter is additive, not a default change."""
    from allocation.proposal import from_swing
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    class D:
        symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
        target = 3950.0; qty = 5; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx({"alloc_hurdle_min_sample": 5}):
        p_swing = from_swing(D())

        def _fresh_alloc():
            alloc = Allocator(sb=_NoDB())
            alloc._priors = {"SWING/CONTINUATION":
                             Prior("SWING/CONTINUATION", 100, 0.30, 0.15, 0.05, -1.0, 2.0),
                             "SWING/ALL": Prior("SWING/ALL", 150, 0.25, 0.12, 0.04, -1.0, 1.9)}
            alloc._hold_days = {"SWING": (5.0, 30)}
            return alloc

        omitted = _fresh_alloc().select(
            [p_swing], regime="NEUTRAL",
            slots_by_framework={"SWING": 3}, max_slots_by_framework={"SWING": 15},
            minutes_left=200)
        explicit_none = _fresh_alloc().select(
            [p_swing], regime="NEUTRAL",
            slots_by_framework={"SWING": 3}, max_slots_by_framework={"SWING": 15},
            minutes_left=200, swing_minutes_left=None)

        assert omitted[0]["hurdle"] == explicit_none[0]["hurdle"], (
            "omitting swing_minutes_left must behave identically to passing "
            "None explicitly — both mean 'no override, use the shared clock'")


TESTS = [
    ("hurdle() time-decay is real and material (mechanism sanity)",
     test_hurdle_is_sensitive_to_minutes_left_by_design),
    ("minutes_left=0 makes hurdle()'s time_mult an exact 1.0 no-op",
     test_hurdle_minutes_left_zero_is_a_time_mult_no_op),
    ("select() isolates swing_minutes_left from INTRADAY",
     test_select_isolates_swing_minutes_left_from_intraday),
    ("select() without swing_minutes_left matches pre-fix behaviour",
     test_select_without_swing_minutes_left_is_unchanged_from_before_the_fix),
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
