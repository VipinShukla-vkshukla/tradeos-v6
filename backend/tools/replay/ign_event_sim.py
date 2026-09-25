"""
Trade simulation over minute bars for the IGN event study. Pure numpy, no I/O.

One trade = a signal at bar i (its CLOSE is known), an entry rule, a stop, and exits. Everything
that happens is on bars strictly after i. Two implementations of the same rules:

    simulate_ref   bar-by-bar loop, written to be read. The specification.
    simulate       vectorised, ~50x faster. What the study runs.

tests/test_ign_event_sim.py checks the two agree on hundreds of random paths and policies, so a
speed-up can never quietly change what a trade means.

RULES (long shown; short is the mirror — prices are sign-flipped so one code path serves both)
  entry     next_open  fill at bar i+1's open.
            pullback   a buy limit `pb_pct`% below bar i's close, live for `wait` bars from i+1.
                       Fills on the first bar whose low is STRICTLY below the limit, at
                       min(limit, that bar's open). No touch, no trade.
  stop      struct     lowest low of the last `stop_arg` bars through i, less STOP_BUFFER — IGN's own.
            pct        `stop_arg`% below the fill.
            A stop at or above the fill is not a trade (None). Risk = fill - stop, per share.
  in a bar  the stop is checked BEFORE the target, so a bar that spans both counts as a loss.
            A stop hit exits at the stop, or at the open if the bar gaps through it. A target hit
            exits at the target, or at the open if the bar gaps past it.
  be        once a bar's high reaches fill + be_after_r x risk, the stop moves to the fill from the
            NEXT bar on (never the same bar: the order of the high and the low is unknown).
  time      after `max_bars` bars held, exit at that bar's close.
  end       the last bar is LAST_EXIT_IDX (15:14), before the 15:15 square-off.
Costs are NOT applied here; gross percent in, the caller subtracts its cost model.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

from tools.replay.ign_event_features import DayBars, LAST_EXIT_IDX, STOP_BUFFER


class Policy(NamedTuple):
    side: int = 1                      # +1 long, -1 short
    entry: str = "next_open"           # next_open | pullback
    pb_pct: float = 0.0
    wait: int = 0
    stop: str = "struct"               # struct | pct
    stop_arg: float = 20.0             # lookback bars (struct) or percent (pct)
    target_r: float | None = None
    max_bars: int | None = None
    be_after_r: float | None = None


def _signed(d: DayBars, side: int):
    """Long-equivalent frame: for a short, prices are negated and high/low swap roles."""
    if side == 1:
        return d.o, d.h, d.l, d.c
    if side == -1:
        return -d.o, -d.l, -d.h, -d.c
    raise ValueError(f"side must be +1 or -1, got {side!r}")


def _stop_signed(d: DayBars, i: int, pol: Policy, fill_s: float, side: int) -> float:
    if pol.stop == "struct":
        k = int(pol.stop_arg)
        if side == 1:
            return float(d.l[max(0, i - k + 1): i + 1].min()) * (1.0 - STOP_BUFFER)
        return -float(d.h[max(0, i - k + 1): i + 1].max()) * (1.0 + STOP_BUFFER)
    if pol.stop == "pct":
        return fill_s - abs(fill_s) * pol.stop_arg / 100.0
    raise ValueError(f"stop must be 'struct' or 'pct', got {pol.stop!r}")


def _entry_ref(d: DayBars, i: int, pol: Policy, o, h, l, c):
    """(entry index, entry price) in the signed frame, or None."""
    if pol.entry == "next_open":
        e = i + 1
        if e >= d.n:
            return None
        return e, float(o[e])
    if pol.entry == "pullback":
        limit = float(c[i]) * (1.0 - pol.pb_pct / 100.0) if pol.side == 1 else float(c[i]) * (1.0 + pol.pb_pct / 100.0)
        limit_s = limit if pol.side == 1 else -limit
        for j in range(i + 1, min(i + pol.wait, LAST_EXIT_IDX, d.n - 1) + 1):
            if l[j] < limit_s:
                return j, min(limit_s, float(o[j]))
        return None
    raise ValueError(f"entry must be 'next_open' or 'pullback', got {pol.entry!r}")


def simulate_ref(d: DayBars, i: int, pol: Policy) -> dict | None:
    """The specification: one bar at a time."""
    o, h, l, c = _signed(d, pol.side)
    ent = _entry_ref(d, i, pol, o, h, l, c)
    if ent is None:
        return None
    e, fill = ent
    stop = _stop_signed(d, i, pol, fill, pol.side)
    risk = fill - stop
    if risk <= 0:
        return None
    end = min(d.n - 1, LAST_EXIT_IDX)
    if end < e:
        return None
    target = fill + pol.target_r * risk if pol.target_r else None
    be_level = fill + pol.be_after_r * risk if pol.be_after_r else None
    cur, moved = stop, False
    exit_idx = exit_px = reason = None
    for j in range(e, end + 1):
        if l[j] <= cur:
            exit_idx, exit_px, reason = j, (cur if o[j] >= cur else float(o[j])), "STOP"
            break
        if target is not None and h[j] >= target:
            exit_idx, exit_px, reason = j, max(target, float(o[j])), "TARGET"
            break
        if be_level is not None and not moved and h[j] >= be_level:
            cur, moved = max(cur, fill), True          # from the NEXT bar
        if pol.max_bars and j - e + 1 >= pol.max_bars:
            exit_idx, exit_px, reason = j, float(c[j]), "TIME"
            break
        if j == end:
            exit_idx, exit_px, reason = j, float(c[j]), "EOD"
    return _pack(pol, e, fill, stop, risk, exit_idx, exit_px, reason)


def _pack(pol: Policy, e: int, fill: float, stop: float, risk: float,
          exit_idx: int, exit_px: float, reason: str) -> dict:
    sgn = pol.side
    entry = sgn * fill
    return {"e": e, "entry": entry, "stop": sgn * stop, "risk_pct": risk / abs(fill) * 100.0,
            "exit_idx": exit_idx, "exit": sgn * exit_px, "reason": reason,
            "gross_pct": (exit_px - fill) / abs(fill) * 100.0, "bars": exit_idx - e + 1}


# ── the fast implementation ─────────────────────────────────────────────────

def simulate(d: DayBars, i: int, pol: Policy) -> dict | None:
    o, h, l, c = _signed(d, pol.side)
    if pol.entry == "next_open":
        e = i + 1
        if e >= d.n:
            return None
        fill = float(o[e])
    elif pol.entry == "pullback":
        limit = float(c[i]) * (1.0 - pol.pb_pct / 100.0) if pol.side == 1 else float(c[i]) * (1.0 + pol.pb_pct / 100.0)
        limit_s = limit if pol.side == 1 else -limit
        last = min(i + pol.wait, LAST_EXIT_IDX, d.n - 1)
        if last < i + 1:
            return None
        hit = l[i + 1: last + 1] < limit_s
        if not hit.any():
            return None
        e = i + 1 + int(np.argmax(hit))
        fill = min(limit_s, float(o[e]))
    else:
        raise ValueError(f"entry must be 'next_open' or 'pullback', got {pol.entry!r}")
    stop = _stop_signed(d, i, pol, fill, pol.side)
    risk = fill - stop
    if risk <= 0:
        return None
    end = min(d.n - 1, LAST_EXIT_IDX)
    if end < e:
        return None
    oo, hh, ll, cc = o[e:end + 1], h[e:end + 1], l[e:end + 1], c[e:end + 1]
    n = len(cc)
    big = n + 1
    stop_arr = np.full(n, stop)
    if pol.be_after_r:
        be_hit = hh >= fill + pol.be_after_r * risk
        if be_hit.any():
            fb = int(np.argmax(be_hit))
            # a hit on bar fb only protects bars AFTER it
            # (and only if no stop/target/time exit came first: the min below resolves that)
            stop_arr[fb + 1:] = max(stop, fill)
    s_hit = ll <= stop_arr
    s_idx = int(np.argmax(s_hit)) if s_hit.any() else big
    t_idx = big
    target = None
    if pol.target_r:
        target = fill + pol.target_r * risk
        t_hit = hh >= target
        t_idx = int(np.argmax(t_hit)) if t_hit.any() else big
    time_idx = pol.max_bars - 1 if pol.max_bars and pol.max_bars - 1 < n else big
    eod_idx = n - 1
    k = min(s_idx, t_idx, time_idx, eod_idx)
    if k == s_idx:
        cur = float(stop_arr[k])
        px, reason = (cur if oo[k] >= cur else float(oo[k])), "STOP"
    elif k == t_idx:
        px, reason = max(target, float(oo[k])), "TARGET"
    elif k == time_idx:
        px, reason = float(cc[k]), "TIME"
    else:
        px, reason = float(cc[k]), "EOD"
    return _pack(pol, e, fill, stop, risk, e + k, px, reason)


def net_r(res: dict, cost_pct: float) -> float:
    """Net R for one simulated trade: gross percent less a round-trip cost in percent of the
    fill, over the risk percent."""
    return (res["gross_pct"] - cost_pct) / res["risk_pct"]
