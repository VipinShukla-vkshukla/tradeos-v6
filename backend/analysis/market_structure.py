"""
Swing structure — higher highs, lower lows, and the moment a trend turns.

THE GAP THIS FILLS
------------------
Every engine in this system reasons about STATIC levels: the opening range high,
the previous day's high, VWAP, the top of a coil. None of them knows the
SEQUENCE those levels sit in, and the sequence is what price action is actually
about.

Concretely: a break of the opening range high means one thing when the stock has
been making higher highs all morning, and something completely different when it
has been making lower highs and has just reclaimed one level. Both look
identical to ORB today. The first is continuation; the second is a reversal
attempt, which is the highest-quality entry in price action and also the most
common trap.

Shared between swing and intraday because the concept does not change with the
timeframe — only the bars do. A daily chart making higher highs and a five-minute
chart making higher highs are the same statement about who is in control.

WHAT A "HIGHER HIGH" IS AND IS NOT
-----------------------------------
Not simply "today's high beat yesterday's". A structural higher high is a
confirmed PIVOT that exceeds the previous confirmed pivot high. A pivot needs
bars on BOTH sides to confirm it, which means the most recent few bars can never
form one yet — and that lag is the entire reason this is trustworthy. A "higher
high" declared on the current bar is just a price that has not been rejected
YET.

THE REVERSAL SEQUENCE, IN ORDER
-------------------------------
    1. downtrend         lower highs and lower lows
    2. higher HIGH       the first break of the sequence — NOT yet a reversal.
                         Most of these fail. This is a warning, not an entry.
    3. higher LOW        the confirmation. The pullback held above the prior
                         low, which means the sellers who made that low are
                         gone rather than merely absent.
    4. break of the HH   the entry the swing engines would already recognise

The system's job is to distinguish step 2 from step 3, because acting on step 2
is how a downtrend takes your money twice.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg_float, cfg_int

UPTREND      = "UPTREND"        # HH + HL
DOWNTREND    = "DOWNTREND"      # LH + LL
REVERSAL_UP  = "REVERSAL_UP"    # first HH after a downtrend, low not yet confirmed
CONFIRMED_UP = "CONFIRMED_UP"   # HH followed by a HL — the real turn
RANGE        = "RANGE"          # no consistent sequence
UNKNOWN      = "UNKNOWN"        # not enough bars to say


@dataclass
class Pivot:
    index: int
    price: float
    kind: str          # HIGH | LOW


@dataclass
class Structure:
    state: str
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    last_pivot_high: float | None = None
    last_pivot_low: float | None = None
    detail: str = ""

    @property
    def long_ok(self) -> bool:
        """
        May a long be taken on structure alone?

        REVERSAL_UP is deliberately EXCLUDED. A first higher high inside a
        downtrend is the single most seductive pattern in price action and the
        one that fails most often — the low that follows it decides whether the
        trend turned, and that low has not happened yet.
        """
        return self.state in (UPTREND, CONFIRMED_UP)

    @property
    def is_warning(self) -> bool:
        return self.state in (DOWNTREND, REVERSAL_UP)


def find_pivots(highs: list[float], lows: list[float], k: int | None = None) -> list[Pivot]:
    """
    Fractal pivots: a high with k lower highs on each side, and the mirror for lows.

    The last k bars can never form a pivot, by construction. That lag is not a
    limitation to work around — it is what makes a pivot meaningful. A high that
    has not yet been rejected is not a pivot, it is the current price.
    """
    k = k or cfg_int("structure_pivot_k", 2)
    out: list[Pivot] = []
    n = len(highs)
    if n < 2 * k + 1:
        return out
    for i in range(k, n - k):
        window = range(i - k, i + k + 1)
        if all(highs[i] >= highs[j] for j in window) and \
           any(highs[i] > highs[j] for j in window if j != i):
            out.append(Pivot(i, highs[i], "HIGH"))
        if all(lows[i] <= lows[j] for j in window) and \
           any(lows[i] < lows[j] for j in window if j != i):
            out.append(Pivot(i, lows[i], "LOW"))
    out.sort(key=lambda p: p.index)
    return out


def classify(highs: list[float], lows: list[float],
             k: int | None = None) -> Structure:
    """
    Read the sequence of pivots and name the trend.

    Pure and timeframe-agnostic: pass daily bars for a swing view or intraday
    bars for a session view, and the meaning is the same.
    """
    pivots = find_pivots(highs, lows, k)
    ph = [p.price for p in pivots if p.kind == "HIGH"]
    pl = [p.price for p in pivots if p.kind == "LOW"]

    if len(ph) < 2 or len(pl) < 2:
        return Structure(UNKNOWN, ph, pl,
                         ph[-1] if ph else None, pl[-1] if pl else None,
                         "not enough confirmed pivots to read a sequence")

    tol = cfg_float("structure_tolerance_pct", 0.10) / 100.0
    hh = ph[-1] > ph[-2] * (1 + tol)
    lh = ph[-1] < ph[-2] * (1 - tol)
    hl = pl[-1] > pl[-2] * (1 + tol)
    ll = pl[-1] < pl[-2] * (1 - tol)

    if hh and hl:
        return Structure(UPTREND, ph, pl, ph[-1], pl[-1],
                         f"higher high {ph[-1]:.2f} over {ph[-2]:.2f} and "
                         f"higher low {pl[-1]:.2f} over {pl[-2]:.2f}")
    if lh and ll:
        return Structure(DOWNTREND, ph, pl, ph[-1], pl[-1],
                         f"lower high {ph[-1]:.2f} under {ph[-2]:.2f} and "
                         f"lower low {pl[-1]:.2f} under {pl[-2]:.2f}")

    # THE CASE THIS MODULE EXISTS FOR: a higher high while the lows are still
    # making lower lows, or have not yet made a higher one. The trend has been
    # broken but not yet turned.
    if hh and not hl:
        # Did a low form AFTER the most recent pivot high? If it did and it held
        # above the prior low, the reversal is confirmed rather than attempted.
        last_high_idx = max((p.index for p in pivots if p.kind == "HIGH"), default=-1)
        lows_after = [p for p in pivots if p.kind == "LOW" and p.index > last_high_idx]
        if lows_after and len(pl) >= 2 and lows_after[-1].price > pl[-2] * (1 + tol):
            return Structure(CONFIRMED_UP, ph, pl, ph[-1], lows_after[-1].price,
                             f"higher high {ph[-1]:.2f} CONFIRMED by a higher low "
                             f"{lows_after[-1].price:.2f} above {pl[-2]:.2f}")
        return Structure(REVERSAL_UP, ph, pl, ph[-1], pl[-1],
                         f"higher high {ph[-1]:.2f} over {ph[-2]:.2f}, but the low "
                         f"has not confirmed — {pl[-1]:.2f} vs {pl[-2]:.2f}. The "
                         f"trend is broken, not yet turned")

    return Structure(RANGE, ph, pl, ph[-1], pl[-1],
                     "no consistent sequence — highs and lows are not trending")


def from_bars(bars) -> Structure:
    """Convenience for intraday Bar objects."""
    if not bars:
        return Structure(UNKNOWN, detail="no bars")
    return classify([b.high for b in bars], [b.low for b in bars])


# ── Framework-specific parameters ───────────────────────────────────────────
# The CONCEPT is identical across timeframes; the SETTINGS cannot be. A daily
# bar is a day of agreed price discovery, while a one-minute bar is frequently
# noise — so the same pivot width applied to both would either miss every swing
# turn or invent an intraday one every few minutes.
#
#   pivot_k     how many bars must confirm a turn. Wider intraday, because
#               minute bars alternate constantly and a k=2 fractal on them
#               marks noise as structure.
#   tolerance   how much higher a high must be to count as HIGHER. Wider for
#               swing, where a 0.1% difference between two daily pivots is not
#               a meaningful statement about control.
#   lookback    how many bars to read. Swing wants weeks of daily structure;
#               intraday wants THIS SESSION, because yesterday's intraday
#               structure has no bearing on today's.

_FRAMEWORK_DEFAULTS = {
    "SWING":    {"pivot_k": 2, "tolerance_pct": 0.50, "lookback": 60},
    "INTRADAY": {"pivot_k": 3, "tolerance_pct": 0.15, "lookback": 120},
}


def params_for(framework: str) -> dict:
    """Structure settings for a framework, overridable from system_config."""
    fw = (framework or "SWING").upper()
    d = _FRAMEWORK_DEFAULTS.get(fw, _FRAMEWORK_DEFAULTS["SWING"])
    pre = "structure_intraday_" if fw == "INTRADAY" else "structure_swing_"
    return {
        "pivot_k":       cfg_int(f"{pre}pivot_k", d["pivot_k"]),
        "tolerance_pct": cfg_float(f"{pre}tolerance_pct", d["tolerance_pct"]),
        "lookback":      cfg_int(f"{pre}lookback", d["lookback"]),
        "allow_reversal": cfg_float(f"{pre}allow_reversal", 0.0) >= 1.0,
    }


def for_framework(framework: str, highs: list[float],
                  lows: list[float]) -> tuple[Structure, dict]:
    """
    Classify with the settings that framework should use.

    Returns (structure, params) so a caller can report WHICH rules produced the
    verdict — "downtrend on 3-bar intraday pivots" and "downtrend on 2-bar daily
    pivots" are different claims and should not be reported identically.
    """
    p = params_for(framework)
    n = p["lookback"]
    h, l = highs[-n:], lows[-n:]

    # classify() reads its tolerance from a global key; set the framework's
    # value for this call rather than duplicating the comparison logic.
    import analysis.market_structure as _self
    prev = _self.cfg_float
    try:
        _self.cfg_float = lambda k, d=0.0: (p["tolerance_pct"]
                                            if k == "structure_tolerance_pct" else prev(k, d))
        s = classify(h, l, p["pivot_k"])
    finally:
        _self.cfg_float = prev
    return s, p


def gate_for_framework(framework: str, highs: list[float],
                       lows: list[float]) -> tuple[bool, str, Structure]:
    """One call: classify for the framework and decide whether a long may go."""
    s, p = for_framework(framework, highs, lows)
    ok, why = gate_long(s, allow_reversal=p["allow_reversal"])
    return ok, f"[{framework.lower()} structure] {why}", s


def gate_long(struct: Structure, allow_reversal: bool | None = None) -> tuple[bool, str]:
    """
    Should a long be allowed given the structure? Returns (allow, reason).

    A DOWNTREND blocks outright — buying a breakout inside a downtrend is buying
    a lower high, which is the definition of the trade the trend is punishing.

    REVERSAL_UP is configurable and defaults to BLOCKED. It is the highest-
    quality entry in price action once confirmed, and the most expensive one
    before. Waiting for the higher low costs some upside and removes most of the
    failures.
    """
    if struct.state == UNKNOWN:
        # Not enough data is not evidence of weakness. Let the other filters
        # decide rather than blocking on ignorance.
        return True, "structure unknown — not blocking"
    if struct.state == DOWNTREND:
        return False, f"downtrend — {struct.detail}"
    if struct.state == REVERSAL_UP:
        allow = (cfg_float("structure_allow_reversal", 0.0) >= 1.0
                 if allow_reversal is None else allow_reversal)
        if not allow:
            return False, (f"reversal not yet confirmed — {struct.detail}. "
                           f"Waiting for the higher low")
        return True, f"unconfirmed reversal, allowed by config — {struct.detail}"
    return True, struct.detail or struct.state
