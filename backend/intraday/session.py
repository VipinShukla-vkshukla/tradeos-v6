"""
The Indian trading session, as an intraday trader actually experiences it.

A session is not six flat hours. It has phases with genuinely different
character, and a setup that works in one is often actively harmful in another —
the single most common way an intraday system loses money is applying a
momentum rule during the midday drift, or a mean-reversion rule into the
opening auction.

    09:07-09:15  PRE_OPEN     Call auction. Prices here are indicative and
                              routinely gap away at 09:15. No decisions.
    09:15-09:30  OPENING      Maximum volatility, widest spreads, thinnest
                              books. Overnight orders clear here. The opening
                              range forms here — it is INPUT, not a signal.
    09:30-11:00  PRIME        The highest-quality window. Direction from the
                              open has been tested, spreads have normalised,
                              institutional flow is active. Breakouts here
                              carry the best follow-through of the day.
    11:00-13:30  DRIFT        Volume decays, ranges compress, false breaks
                              multiply. Breakout strategies are switched OFF
                              here by default; mean reversion to VWAP is the
                              only thing that reliably works.
    13:30-15:00  AFTERNOON    Volume returns. Trends that survived the drift
                              often resume; new positions get progressively
                              less time to work.
    15:00-15:20  CLOSING      No new entries. MIS positions are squared off by
                              the broker from ~15:20 — being flat before that
                              is a choice; being squared off is not.
    15:20-15:30  SQUARE_OFF   Broker auto-closes MIS. Anything still open is
                              exited at whatever the book offers.

WHY THE WINDOWS ARE CONFIGURABLE BUT THE PHASES ARE NOT
-------------------------------------------------------
The boundaries move with market regime and can be tuned. The existence of the
phases is structural — it comes from when the exchange opens, when institutions
trade, and when the broker force-closes — so strategies declare which phases
they are valid in, and the engine enforces that rather than each strategy
re-checking the clock.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import IST, cfg_int

PRE_OPEN   = "PRE_OPEN"
OPENING    = "OPENING"
PRIME      = "PRIME"
DRIFT      = "DRIFT"
AFTERNOON  = "AFTERNOON"
CLOSING    = "CLOSING"
SQUARE_OFF = "SQUARE_OFF"
CLOSED     = "CLOSED"

# Ordered boundaries. Each entry is (phase, start).
_BOUNDS = [
    (PRE_OPEN,   dtime(9, 7)),
    (OPENING,    dtime(9, 15)),
    (PRIME,      dtime(9, 30)),
    (DRIFT,      dtime(11, 0)),
    (AFTERNOON,  dtime(13, 30)),
    (CLOSING,    dtime(15, 0)),
    (SQUARE_OFF, dtime(15, 20)),
]
MARKET_CLOSE = dtime(15, 30)

# Phases in which a NEW intraday position may be opened at all. Entering during
# OPENING means paying the widest spread of the day for the privilege of being
# wrong first; entering during CLOSING means asking a thesis to resolve in under
# twenty minutes against a forced exit.
TRADEABLE = {PRIME, DRIFT, AFTERNOON}


@dataclass(frozen=True)
class SessionState:
    phase: str
    minutes_since_open: int
    minutes_to_squareoff: int
    can_enter: bool
    reason: str


def phase_at(now: datetime | None = None) -> str:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return CLOSED
    t = now.time()
    if t < _BOUNDS[0][1] or t > MARKET_CLOSE:
        return CLOSED
    current = CLOSED
    for name, start in _BOUNDS:
        if t >= start:
            current = name
    return current


def session_state(now: datetime | None = None) -> SessionState:
    """Everything a strategy needs to know about the clock, computed once."""
    now = now or datetime.now(IST)
    ph = phase_at(now)
    open_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
    sq_dt   = now.replace(hour=15, minute=20, second=0, microsecond=0)
    since = int((now - open_dt).total_seconds() // 60)
    to_sq = int((sq_dt - now).total_seconds() // 60)

    # A position needs room to work. Entering with fifteen minutes left is not a
    # trade, it is a coin flip that pays costs either way.
    min_runway = cfg_int("intraday_min_runway_min", 45)

    if ph not in TRADEABLE:
        return SessionState(ph, since, to_sq, False,
                            f"{ph.lower().replace('_', ' ')} — entries are not taken in this phase")
    if to_sq < min_runway:
        return SessionState(ph, since, to_sq, False,
                            f"only {to_sq} min to square-off, under the {min_runway} min minimum runway")
    return SessionState(ph, since, to_sq, True, f"{ph} — {to_sq} min of runway")


def is_squaring_off(now: datetime | None = None) -> bool:
    return phase_at(now) == SQUARE_OFF


def minutes_to_close(now: datetime | None = None) -> int:
    now = now or datetime.now(IST)
    close_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return max(0, int((close_dt - now).total_seconds() // 60))
