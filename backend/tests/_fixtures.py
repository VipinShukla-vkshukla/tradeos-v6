"""
Synthetic sessions, shaped like real ones.

WHY THE SHAPES MATTER MORE THAN THE NUMBERS
--------------------------------------------
The first version of the short-engine test built sessions where price had
already run two percent past the level that failed. Every condition correctly
declined them — the stop belongs just above that level, so a late entry carries
an enormous stop and the R:R floor refuses it — and the test read as "the engine
finds nothing" when the engine was working and the SESSIONS were wrong.

That mistake found a real defect (the engine had no chase rule at all), so it
was worth making once. It is not worth making twice, which is why each builder
below states the shape it is constructing and where the trigger sits.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tests import BACKEND  # noqa: F401  — path bootstrap
from config import IST
from intraday.strategies.base import Bar, SymbolContext

OPEN = IST.localize(datetime(2026, 8, 5, 9, 15))


def bars(seq) -> list[Bar]:
    """(open, high, low, close, volume) tuples -> one-minute Bars from the open."""
    return [Bar(OPEN + timedelta(minutes=i), o, h, l, c, v)
            for i, (o, h, l, c, v) in enumerate(seq)]


def session(kind: str) -> list[Bar]:
    """
    Sessions positioned at a FRESH trigger — price still within the chase
    limit of the level that failed, not well past it.
    """
    b = []
    if kind == "trap":
        # Morning weakness sets a low near 98.6; price then pokes THROUGH
        # yesterday's high (100.0) to 101.2 and fails back to just under it.
        for i in range(8):
            b.append((99.6 - i * .13, 99.8 - i * .12, 99.2 - i * .14, 99.4 - i * .13, 9000))
        for i in range(6):
            b.append((98.7 + i * .42, 99.0 + i * .45, 98.6 + i * .40, 98.9 + i * .44, 15000))
        for i in range(6):
            b.append((101.1 - i * .24, 101.2 - i * .22, 100.6 - i * .26, 100.8 - i * .25, 18000))
    elif kind == "vwap_reject":
        # Slides under VWAP, sets the day low, rallies back INTO VWAP and stalls.
        for i in range(10):
            b.append((100.4 - i * .20, 100.5 - i * .19, 99.9 - i * .21, 100.0 - i * .20, 12000))
        for i in range(6):
            b.append((98.1 + i * .14, 98.4 + i * .16, 98.0 + i * .13, 98.3 + i * .15, 9000))
        for i in range(6):
            b.append((99.2 - i * .03, 99.35 - i * .02, 99.0 - i * .05, 99.1 - i * .04, 11000))
    elif kind == "breakdown":
        # A 15-minute opening range, then a break of its low on expanding volume.
        for _ in range(15):
            b.append((100.0, 100.35, 99.65, 100.0, 10000))
        for i in range(6):
            b.append((99.9 - i * .09, 100.0 - i * .08, 99.5 - i * .10, 99.6 - i * .09, 28000))
    else:
        raise ValueError(f"unknown session shape: {kind}")
    return bars(b)


def ctx_for(kind: str, **overrides) -> SymbolContext:
    """A SymbolContext over `session(kind)`, with any field overridable."""
    bs = session(kind)
    vwap = sum(x.close * x.volume for x in bs) / sum(x.volume for x in bs)
    c = SymbolContext(
        symbol="TESTCO", ltp=bs[-1].close, bars=bs, vwap=round(vwap, 2),
        day_open=bs[0].open, day_high=max(x.high for x in bs),
        day_low=min(x.low for x in bs), prev_close=100.6,
        prev_high=100.0, prev_low=97.0, atr_pct_daily=2.2,
        avg_volume_20d=4_000_000, value_cr=180.0, sector="IT",
        rs_vs_index_pct=-0.9, as_of=OPEN,
    )
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


#: A clean daily row — liquid, unflagged, ordinary delivery.
CLEAN_STOCK = {"value_cr": 180.0, "delivery_pct": 42.0,
               "asm_flag": False, "fo_ban_flag": False}

#: The intraday exit policy, fully populated. Kept here rather than built per
#: test because a missing key raises KeyError deep inside the ladder and reads
#: like a logic failure rather than a test-setup one.
EXIT_POLICY = {
    "squareoff_buffer": 5, "must_exit_time": "15:15", "check_invalidation": True,
    "use_setup_target": True, "target_r": 2.0, "partial_book_r": 1.0,
    "partial_book_pct": 50.0, "move_to_breakeven": True, "breakeven_at_r": 1.0,
    "cost_buffer_pct": 0.21, "trail_after_r": 1.5, "trail_r": 1.0,
    "giveback_pct": 30.0, "giveback_min_r": 1.0,
    # 999 disables the time stop so the rungs under it stay reachable; a test
    # that wants the time stop sets it explicitly.
    "time_stop_min": 999, "time_stop_min_r": 0.3, "time_stop_max_r": 0.5,
}

#: Mid-session, so no square-off rung fires and the whole ladder is exercised.
MIDDAY = IST.localize(datetime(2026, 8, 5, 12, 0))


def long_pos(**kw) -> dict:
    p = {"entry_price": 100.0, "planned_stop": 99.0, "planned_target": 103.0,
         "current_qty": 10, "direction": "LONG"}
    p.update(kw)
    return p


def short_pos(**kw) -> dict:
    """The exact mirror of long_pos about the entry price."""
    p = {"entry_price": 100.0, "planned_stop": 101.0, "planned_target": 97.0,
         "current_qty": 10, "direction": "SHORT"}
    p.update(kw)
    return p
