"""
IGN event-study trade simulator — tools/replay/ign_event_sim.py.

Every scenario below is worked out by hand in its comment (never copied from the implementation).
Then the fast vectorised simulator is required to agree with the bar-by-bar reference on hundreds
of random paths and policies, and the tests check that nothing after the exit — and nothing the
model could only know later — reaches a trade's result.
"""

from __future__ import annotations

import numpy as np

from tools.replay import ign_event_sim as S
from tools.replay.ign_event_features import DayBars

I = 50                      # the signal bar; the trade can first fill on bar 51


def _flat(n=375, px=100.0):
    a = lambda: np.full(n, float(px))
    return DayBars(a(), a(), a(), a(), np.full(n, 1000.0))


def _set(d, j, o=None, h=None, l=None, c=None):
    arrs = [d.o.copy(), d.h.copy(), d.l.copy(), d.c.copy(), d.v.copy()]
    for k, v in enumerate((o, h, l, c)):
        if v is not None:
            arrs[k][j] = v
    return DayBars(*arrs)


def _both(d, pol, i=I):
    """Run both simulators and require them to agree before returning the result."""
    a, b = S.simulate_ref(d, i, pol), S.simulate(d, i, pol)
    assert (a is None) == (b is None), (a, b)
    if a is not None:
        for k in a:
            assert (abs(a[k] - b[k]) < 1e-9) if isinstance(a[k], float) else a[k] == b[k], (k, a, b)
    return a


P = lambda **kw: S.Policy(stop="pct", stop_arg=1.0, **kw)      # fill 100 -> stop 99, risk exactly 1%


def test_a_target_is_hit_at_the_target_price():
    d = _set(_flat(), 55, h=102.5)                          # target = 100 + 2 x 1 = 102
    r = _both(d, P(target_r=2.0))
    assert (r["reason"], r["exit"], r["exit_idx"], r["bars"]) == ("TARGET", 102.0, 55, 5), r
    assert abs(r["gross_pct"] - 2.0) < 1e-9 and abs(r["risk_pct"] - 1.0) < 1e-9
    assert abs(S.net_r(r, 0.2) - 1.8) < 1e-9, "(2.0 - 0.2) / 1.0"


def test_an_exact_touch_of_the_target_or_the_stop_counts():
    hit = _both(_set(_flat(), 55, h=102.0), P(target_r=2.0))
    assert (hit["reason"], hit["exit"]) == ("TARGET", 102.0), "a high exactly at the target fills it"
    out = _both(_set(_flat(), 55, l=99.0), P(target_r=2.0))
    assert (out["reason"], out["exit"]) == ("STOP", 99.0), "a low exactly at the stop stops it out"


def test_a_stop_is_hit_at_the_stop_price():
    d = _set(_flat(), 53, l=98.9)                           # 98.9 <= 99
    r = _both(d, P(target_r=2.0))
    assert (r["reason"], r["exit"], r["gross_pct"]) == ("STOP", 99.0, -1.0), r


def test_a_bar_spanning_both_counts_as_a_loss():
    d = _set(_flat(), 53, h=103.0, l=98.5)                  # touches 99 and 102 in one bar
    r = _both(d, P(target_r=2.0))
    assert r["reason"] == "STOP" and r["gross_pct"] == -1.0, "pessimistic: stop before target"


def test_a_gap_through_the_stop_exits_at_the_open():
    d = _set(_flat(), 53, o=98.0, h=98.2, l=97.5, c=98.1)
    r = _both(d, P(target_r=2.0))
    assert (r["reason"], r["exit"], r["gross_pct"]) == ("STOP", 98.0, -2.0), r


def test_a_gap_past_the_target_exits_at_the_open():
    d = _set(_flat(), 53, o=103.0, h=103.5, l=102.9, c=103.2)
    r = _both(d, P(target_r=2.0))
    assert (r["reason"], r["exit"], r["gross_pct"]) == ("TARGET", 103.0, 3.0), r


def _lifted(d, frm, px=100.5):
    """Everything from bar `frm` on sits flat ABOVE the fill, so a breakeven stop at the fill is
    never touched by accident."""
    o, h, l, c, v = (a.copy() for a in d)
    for a in (o, h, l, c):
        a[frm:] = px
    return DayBars(o, h, l, c, v)


def test_breakeven_protects_from_the_next_bar_only():
    d = _lifted(_set(_flat(), 54, h=101.2, l=99.8), 55)     # bar 54 reaches +1R (101) and dips to the fill
    d = _set(d, 56, l=99.5)                                 # above the 99 stop, below the 100 fill
    d = _set(d, 359, c=100.4)
    kept = _both(d, P(be_after_r=1.0))
    assert (kept["reason"], kept["exit_idx"], kept["exit"]) == ("STOP", 56, 100.0), (
        "the stop moved to the fill after bar 54, so bar 56's dip to 99.5 exits at 100")
    assert kept["gross_pct"] == 0.0
    plain = _both(d, P())
    assert (plain["reason"], plain["exit"]) == ("EOD", 100.4), "without the move, bar 56 does not stop it"
    same_bar = _both(_lifted(_set(_flat(), 54, h=101.2, l=99.8), 55), P(be_after_r=1.0))
    assert same_bar["reason"] == "EOD", "the low on the very bar that reached +1R must not stop it out"


def test_a_time_exit_uses_the_close_of_the_last_bar_held():
    d = _set(_flat(), 60, c=100.7)                          # bars 51..60 = 10 bars
    r = _both(d, P(max_bars=10))
    assert (r["reason"], r["exit_idx"], r["bars"], r["exit"]) == ("TIME", 60, 10, 100.7), r
    assert abs(r["gross_pct"] - 0.7) < 1e-9


def test_the_end_of_day_exit_is_the_last_close_before_the_squareoff():
    d = _set(_flat(), 359, c=101.1)
    d = _set(d, 360, o=555.0, h=999.0, l=1.0, c=555.0)      # after square-off: must not exist for a trade
    r = _both(d, P())
    assert (r["reason"], r["exit_idx"], r["exit"]) == ("EOD", 359, 101.1), r


def test_the_last_bar_that_can_be_entered_and_the_first_that_cannot():
    d = _set(_flat(), 359, c=100.5)
    one = _both(d, P(), i=358)
    assert one is not None and one["bars"] == 1 and one["reason"] == "EOD"
    assert _both(d, P(), i=359) is None, "the next open would be 15:15 or later"
    assert _both(d, P(), i=370) is None


def test_the_structural_stop_is_the_lowest_low_of_the_lookback_less_the_buffer():
    d = _set(_flat(), 40, l=99.0)                           # inside bars 31..50
    r = _both(d, S.Policy(stop="struct", stop_arg=20))
    assert abs(r["stop"] - 99.0 * (1 - 0.0012)) < 1e-9, r["stop"]
    assert abs(r["risk_pct"] - (100 - 99.0 * 0.9988)) < 1e-9
    older = _set(_flat(), 25, l=95.0)                       # bar 25 is outside the last 20 (31..50)
    assert abs(_both(older, S.Policy(stop="struct", stop_arg=20))["stop"] - 100 * 0.9988) < 1e-9
    wide = _both(older, S.Policy(stop="struct", stop_arg=30))
    assert abs(wide["stop"] - 95.0 * 0.9988) < 1e-9, "a 30-bar lookback reaches bar 25"


def test_a_stop_at_or_above_the_fill_is_not_a_trade():
    d = _set(_flat(px=100.0), 51, o=98.0, h=98.0, l=98.0, c=98.0)   # gaps below every recent low
    assert _both(d, S.Policy(stop="struct", stop_arg=20)) is None


def test_pullback_fills_below_the_limit_and_never_on_a_touch():
    d = _set(_flat(), 52, l=99.4)                           # limit = 100 x (1 - 0.5%) = 99.5
    r = _both(d, S.Policy(entry="pullback", pb_pct=0.5, wait=5, stop="pct", stop_arg=1.0))
    assert (r["e"], r["entry"]) == (52, 99.5), r
    assert abs(r["stop"] - 99.5 * 0.99) < 1e-9
    touch = _set(_flat(), 52, l=99.5)
    assert _both(touch, S.Policy(entry="pullback", pb_pct=0.5, wait=5, stop="pct", stop_arg=1.0)) is None, (
        "trading through the limit is required; touching it is not a fill")
    late = _set(_flat(), 57, l=99.0)                        # wait=5 covers bars 51..55
    assert _both(late, S.Policy(entry="pullback", pb_pct=0.5, wait=5, stop="pct", stop_arg=1.0)) is None


def test_a_pullback_that_gaps_below_the_limit_fills_at_the_open():
    d = _set(_flat(), 52, o=99.0, h=99.2, l=98.9, c=99.1)
    r = _both(d, S.Policy(entry="pullback", pb_pct=0.5, wait=5, stop="pct", stop_arg=1.0))
    assert r["entry"] == 99.0 and r["e"] == 52, r


def test_a_pullback_through_the_structural_stop_is_not_a_trade():
    d = _set(_flat(), 52, l=97.0, o=98.0, c=98.0)           # limit 2% down = 98.0; stop ~99.88
    assert _both(d, S.Policy(entry="pullback", pb_pct=2.0, wait=5, stop="struct", stop_arg=20)) is None


def test_the_short_side_mirrors_the_long_side():
    d = _set(_flat(), 55, l=97.8)                           # short from 100, stop 101, target 98
    r = _both(d, P(side=-1, target_r=2.0))
    assert (r["reason"], r["exit"], r["entry"], r["stop"]) == ("TARGET", 98.0, 100.0, 101.0), r
    assert abs(r["gross_pct"] - 2.0) < 1e-9 and abs(r["risk_pct"] - 1.0) < 1e-9
    stopped = _both(_set(_flat(), 53, h=101.2), P(side=-1, target_r=2.0))
    assert (stopped["reason"], stopped["exit"], stopped["gross_pct"]) == ("STOP", 101.0, -1.0), stopped
    gap = _both(_set(_flat(), 53, o=102.0, h=102.5, l=101.9, c=102.0), P(side=-1))
    assert (gap["exit"], gap["gross_pct"]) == (102.0, -2.0), "a gap up through a short's stop exits at the open"
    d2 = _set(_flat(), 40, h=101.0)
    s2 = _both(d2, S.Policy(side=-1, stop="struct", stop_arg=20))
    assert abs(s2["stop"] - 101.0 * 1.0012) < 1e-9, "a short's structural stop is above the highest high"


def test_bad_arguments_fail_loudly():
    for bad in (S.Policy(side=0), S.Policy(entry="market"), S.Policy(stop="atr")):
        for fn in (S.simulate, S.simulate_ref):
            try:
                fn(_flat(), I, bad)
            except ValueError:
                continue
            raise AssertionError(f"{fn.__name__} accepted {bad}")


# ── nothing after the exit, nothing later than the signal ───────────────────

def test_bars_after_the_exit_cannot_change_the_result():
    d = _set(_flat(), 55, h=102.5)
    base = S.simulate(d, I, P(target_r=2.0))
    rng = np.random.default_rng(1)
    o, h, l, c, v = (a.copy() for a in d)
    for a in (o, h, l, c):
        a[56:] = rng.uniform(1, 500, len(a) - 56)
    assert S.simulate(DayBars(o, h, l, c, v), I, P(target_r=2.0)) == base


def test_a_next_open_trade_is_independent_of_bars_between_signal_and_fill():
    d = _flat()
    a = S.simulate(d, I, P(target_r=2.0))
    o, h, l, c, v = (x.copy() for x in d)
    c[I] = 250.0           # the signal bar's own close must not set a next-open fill
    b = S.simulate(DayBars(o, h, l, c, v), I, P(target_r=2.0))
    assert a["entry"] == b["entry"] == 100.0


# ── the fast simulator against the reference ────────────────────────────────

def _random_day(rng, n=375):
    ret = rng.normal(0.0, rng.uniform(0.0004, 0.0015), n)
    c = 100.0 * np.exp(np.cumsum(ret))
    o = np.concatenate([[100.0], c[:-1]])
    wick = np.abs(rng.normal(0, 0.0006, n))
    h = np.maximum(o, c) * (1 + wick)
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0006, n)))
    return DayBars(o, h, l, c, np.full(n, 1000.0))


def _random_policy(rng):
    entry = "pullback" if rng.random() < 0.35 else "next_open"
    stop = "pct" if rng.random() < 0.45 else "struct"
    return S.Policy(
        side=int(rng.choice([1, -1])), entry=entry,
        pb_pct=float(rng.choice([0.1, 0.25, 0.5, 1.0])), wait=int(rng.choice([1, 5, 15, 30])),
        stop=stop, stop_arg=float(rng.choice([0.3, 0.75, 1.5, 2.5])) if stop == "pct" else float(rng.choice([5, 10, 20, 30])),
        target_r=rng.choice([None, 0.5, 1.0, 2.0, 3.0]),
        max_bars=rng.choice([None, 5, 30, 60, 200]),
        be_after_r=rng.choice([None, 0.5, 1.0, 1.5]))


def test_the_fast_simulator_agrees_with_the_reference_on_random_paths():
    rng = np.random.default_rng(20260925)
    reasons, none_n, pulled, be_used = {}, 0, 0, 0
    for _ in range(1200):
        d = _random_day(rng)
        i = int(rng.integers(20, 372))
        pol = _random_policy(rng)
        pol = pol._replace(target_r=None if pol.target_r is None else float(pol.target_r),
                           max_bars=None if pol.max_bars is None else int(pol.max_bars),
                           be_after_r=None if pol.be_after_r is None else float(pol.be_after_r))
        a, b = S.simulate_ref(d, i, pol), S.simulate(d, i, pol)
        assert (a is None) == (b is None), (i, pol, a, b)
        if a is None:
            none_n += 1
            continue
        for k in a:
            ok = abs(a[k] - b[k]) < 1e-9 if isinstance(a[k], float) else a[k] == b[k]
            assert ok, (k, i, pol, a, b)
        reasons[a["reason"]] = reasons.get(a["reason"], 0) + 1
        pulled += pol.entry == "pullback"
        be_used += pol.be_after_r is not None
    for r in ("STOP", "TARGET", "TIME", "EOD"):
        assert reasons.get(r, 0) >= 25, f"the comparison must exercise {r}: {reasons}"
    assert none_n >= 20 and pulled >= 100 and be_used >= 100, (none_n, pulled, be_used)


TESTS = [
    ("a target is hit at the target price", test_a_target_is_hit_at_the_target_price),
    ("an exact touch of the target or the stop counts", test_an_exact_touch_of_the_target_or_the_stop_counts),
    ("a stop is hit at the stop price", test_a_stop_is_hit_at_the_stop_price),
    ("a bar spanning both counts as a loss", test_a_bar_spanning_both_counts_as_a_loss),
    ("a gap through the stop exits at the open", test_a_gap_through_the_stop_exits_at_the_open),
    ("a gap past the target exits at the open", test_a_gap_past_the_target_exits_at_the_open),
    ("breakeven protects from the next bar only", test_breakeven_protects_from_the_next_bar_only),
    ("a time exit uses the close of the last bar held", test_a_time_exit_uses_the_close_of_the_last_bar_held),
    ("the end-of-day exit is the last close before square-off",
     test_the_end_of_day_exit_is_the_last_close_before_the_squareoff),
    ("the last enterable bar and the first that cannot",
     test_the_last_bar_that_can_be_entered_and_the_first_that_cannot),
    ("the structural stop is the lowest low of the lookback less the buffer",
     test_the_structural_stop_is_the_lowest_low_of_the_lookback_less_the_buffer),
    ("a stop at or above the fill is not a trade", test_a_stop_at_or_above_the_fill_is_not_a_trade),
    ("pullback fills below the limit and never on a touch",
     test_pullback_fills_below_the_limit_and_never_on_a_touch),
    ("a pullback that gaps below the limit fills at the open",
     test_a_pullback_that_gaps_below_the_limit_fills_at_the_open),
    ("a pullback through the structural stop is not a trade",
     test_a_pullback_through_the_structural_stop_is_not_a_trade),
    ("the short side mirrors the long side", test_the_short_side_mirrors_the_long_side),
    ("bad arguments fail loudly", test_bad_arguments_fail_loudly),
    ("bars after the exit cannot change the result", test_bars_after_the_exit_cannot_change_the_result),
    ("a next-open trade is independent of the signal bar's close",
     test_a_next_open_trade_is_independent_of_bars_between_signal_and_fill),
    ("the fast simulator agrees with the reference on random paths",
     test_the_fast_simulator_agrees_with_the_reference_on_random_paths),
]
