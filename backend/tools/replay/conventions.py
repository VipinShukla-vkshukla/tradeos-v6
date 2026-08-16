"""
WHEN the replay evaluates, and WHAT it is allowed to know at that instant.

THE MEASUREMENT THAT MAKES THIS MODULE NECESSARY
-------------------------------------------------
Every one of the nine engines sets `entry=round(ctx.ltp, 2)`. So the stored
`entry` on a recorded detection **is the live LTP at that instant**, to two
decimals — it is not a derived level, it is the price the tick stream was
showing. That turns "tick versus bar" from a hypothesis into a query, and the
query was run on 2026-08-14, over all 212 recorded dedup keys:

    stored entry equals the close of the bar it was detected in ...    8.5%
    ... equals a close within +/- 1 bar ..............................  10.4%
    ... equals no bar close within +/- 10 bars ....................... 64.6%

The replay's `ltp` is, by construction, a completed bar's close. So on ~91% of
recorded detections the live system decided on a price that **is not any bar
close in the neighbourhood** — a price that exists only inside a minute. No
evaluation cadence over minute bars can produce it, because the number is not in
the data.

WHAT A MINUTE BAR DOES STILL CONTAIN
-------------------------------------
It contains the *envelope*. Every tick inside minute M happened at some price in
`[M.low, M.high]`, and the live daemon sampled that path four times (15 s
cadence, `intraday_eval_interval_s`). So while the exact path is unrecoverable,
the SET of contexts the live system could have seen inside M is bounded:

    bars   : every bar strictly before M   (exactly known — live had these too)
    ltp    : somewhere in [M.low, M.high]  (bounded, not known)
    day_hi : between the value at M-1 and max(that, M.high)
    day_lo : between the value at M-1 and min(that, M.low)

`FULL_OVERLAY` below evaluates at the corners of that envelope. It is therefore
an **upper bound on what live could have detected**, not a reproduction: a
detection it cannot produce could not have been produced by any intra-minute
path, and a detection it does produce may still be one live never sampled. Both
halves of that sentence matter, and §10.2 of the design gets both.

WHY `bars` MUST BE TRUNCATED SEPARATELY FROM `now`
---------------------------------------------------
At 09:16:15 the live context holds bars through 09:15 — the 09:16 bar has not
closed, and `bar_builder` only emits completed bars. So a sub-bar evaluation
point has `now` inside bar M while its bar list must stop *before* M. Passing one
timestamp for both would hand the engine the very bar it is standing inside,
which is the lookahead this package exists to make impossible. `build_context`
therefore takes `upto` separately from `now`, and the forming bar reaches the
context only through `apply_forming_bar`, which can see exactly one bar and
asserts it is the one containing `now`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from intraday.strategies.base import Bar

BAR = timedelta(minutes=1)


@dataclass(frozen=True)
class Convention:
    """
    One answer to "when does the replay look, and what does it see?".

    `sub_bar_samples` names points of the FORMING bar to use as `ltp`. Each is a
    price that genuinely traded inside that minute, so each is a legitimate
    sample of the path — but choosing the extremes is optimistic about which
    samples live actually took, and that is the whole reason this is reported as
    a bound rather than a reproduction.
    """
    name: str
    repeats: int = 0                        # extra same-context looks per minute
    sub_bar_samples: tuple[str, ...] = ()   # "open" | "high" | "low"
    running_extremes: bool = False          # day_high/day_low see the forming bar
    running_vwap: bool = False              # vwap/session_volume see it too
    note: str = ""

    @property
    def is_bound(self) -> bool:
        """
        True when this convention over-approximates the live information set.

        THE LINE IS DRAWN AT *WHICH* SAMPLE, NOT AT "SUB-BAR OR NOT". A minute
        bar's OPEN is the first trade of that minute: at 09:16:15 it has already
        printed, so reading it is reading the past. The HIGH and the LOW may not
        print until 09:16:50, so attributing them to a 09:16:15 look asserts a
        price before it happened. `next_open` is therefore faithful and
        `ltp_bracket`/`full_overlay` are bounds — a distinction worth more than
        it looks, because a bound that clears the acceptance bar is not a pass.
        """
        return any(s != "open" for s in self.sub_bar_samples)


# The shipped conventions. Named, so a report can say which one produced it.
BAR_CLOSE = Convention(
    "bar_close",
    note="one look per completed bar; ltp is that bar's close. The original.")

CADENCE_15S = Convention(
    "cadence_15s", repeats=3,
    note="the live 15 s cadence with NO intra-minute price — four looks at the "
         "identical context. Isolates how much of the gap is cadence alone.")

NEXT_OPEN = Convention(
    "next_open", sub_bar_samples=("open",),
    note="one extra look per minute at the forming bar's OPEN — the first tick "
         "of that minute, a price that has already printed when the look "
         "happens. The only sub-bar sample that is NOT lookahead.")

LTP_BRACKET = Convention(
    "ltp_bracket", sub_bar_samples=("open", "high", "low"),
    note="ltp swept over the forming bar's open/high/low; range and VWAP still "
         "from completed bars. Models a tick LTP without the quote overlay.")

FULL_OVERLAY = Convention(
    "full_overlay", sub_bar_samples=("open", "high", "low"),
    running_extremes=True, running_vwap=True,
    note="ltp swept AND day_high/day_low/vwap/session_volume advanced to include "
         "the whole forming bar — the live quote overlay at its most generous. "
         "An UPPER BOUND on live reachability, not a reproduction.")

ALL: dict[str, Convention] = {c.name: c for c in
                              (BAR_CLOSE, CADENCE_15S, NEXT_OPEN,
                               LTP_BRACKET, FULL_OVERLAY)}


@dataclass(frozen=True)
class EvalPoint:
    """One evaluation: when it happens, what it may read, what price it sees."""
    now: datetime         # the wall clock — feeds phase_at() and ctx.as_of
    upto: datetime        # bars STRICTLY before this may be read
    ltp: float | None     # None = use the last completed bar's close
    forming: Bar | None   # the bar `now` sits inside, or None at a bar boundary
    look: str = "bar_close"   # provenance, carried onto every Detection


def evaluation_points(day_bars: list[Bar], conv: Convention):
    """
    Every instant this convention evaluates at, in order.

    The completed-bar point is always emitted first and is identical across all
    conventions, so every convention is a strict superset of `BAR_CLOSE`'s
    evaluations. That is deliberate: it makes the comparison between them a
    question of what the extra looks add, with nothing taken away.
    """
    for i, bar in enumerate(day_bars):
        close_ts = bar.ts + BAR
        yield EvalPoint(now=close_ts, upto=close_ts, ltp=bar.close, forming=None)

        for k in range(1, conv.repeats + 1):
            # Same context, later clock. Only `phase_at()` and any time-of-day
            # branch inside an engine can see the difference — which is exactly
            # what this convention is built to measure.
            yield EvalPoint(now=close_ts + timedelta(seconds=15 * k),
                            upto=close_ts, ltp=bar.close, forming=None,
                            look="bar_close")

        if not conv.sub_bar_samples or i + 1 >= len(day_bars):
            continue
        nxt = day_bars[i + 1]
        if nxt.ts != close_ts:
            # A gap in the tape (halt, illiquid minute). The next bar is not the
            # minute after this one, so there is no forming bar to sample and
            # inventing one would fabricate a price that never traded.
            continue
        for k, field in enumerate(conv.sub_bar_samples, start=1):
            yield EvalPoint(now=nxt.ts + timedelta(seconds=15 * k),
                            upto=nxt.ts,
                            ltp=float(getattr(nxt, field)),
                            forming=nxt, look=field)
