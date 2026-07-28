"""
The switches that decide whether ANY order may be placed, for either framework.

WHY THIS MOVED OUT OF intraday/
--------------------------------
Order placement was built inside the intraday package because that is where it
was first needed. That made execution look like an intraday feature, and it is
not — it is the same concern for both books. The visible symptom was that
enabling Phase 3 automated intraday exits while swing partials stayed manual,
even though the swing engine had been printing "BOOK 7 of 15" for days.

Placing an order is placing an order. The rails that make it safe do not change
because the thesis was three weeks long instead of three hours.

TWO GATES, NOT ONE
------------------
    intraday_auto_exit   automate exits on INTRADAY positions
    swing_auto_exit      automate exits on SWING positions

Separate because the risk is genuinely different. An intraday exit is bounded —
it happens today, on a position sized for a 0.5% stop. A swing exit disposes of
a position that may represent a large share of the book and has been building
for weeks. Being comfortable with one says nothing about the other, so one
switch would force a decision you have no reason to make.

Both still require phase >= 3.0, the master boolean, and DRY_RUN off.
"""

from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import IST, DRY_RUN, cfg_bool, cfg_float, cfg_int


def autonomy_phase() -> float:
    return cfg_float("intraday_autonomy_phase", 2.0)


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


def orders_enabled() -> bool:
    """
    The master gate. Three independent switches, each failing differently: the
    phase is a deliberate promotion, the boolean is a fast off-switch that needs
    no reasoning about phases, and DRY_RUN keeps the idiom used everywhere else
    in this codebase working here, where it matters most.
    """
    return (autonomy_phase() >= 3.0
            and cfg_bool("intraday_orders_enabled", False)
            and not DRY_RUN)


def auto_exit_enabled(framework: str) -> bool:
    """Per-framework exit automation, on top of the master gate."""
    if not orders_enabled():
        return False
    fw = (framework or "SWING").upper()
    if fw == "INTRADAY":
        return cfg_bool("intraday_auto_exit", False)
    return cfg_bool("swing_auto_exit", False)


# ── Risk rails ──────────────────────────────────────────────────────────────
# Deliberately per-framework: a swing position is typically larger and rarer, so
# a cap tuned for intraday scalping would block every legitimate swing exit.

def max_order_value(framework: str = "INTRADAY") -> float:
    key = ("swing_max_order_value" if (framework or "").upper() == "SWING"
           else "intraday_max_order_value")
    return cfg_float(key, 10000.0)


def max_orders_per_day(framework: str = "INTRADAY") -> int:
    key = ("swing_max_orders_per_day" if (framework or "").upper() == "SWING"
           else "intraday_max_orders_per_day")
    return cfg_int(key, 5)


def max_notional_per_day(framework: str = "INTRADAY") -> float:
    key = ("swing_max_notional_per_day" if (framework or "").upper() == "SWING"
           else "intraday_max_notional_per_day")
    return cfg_float(key, 25000.0)


def gtt_enabled() -> bool:
    return autonomy_phase() >= 2.5 and cfg_bool("intraday_gtt_enabled", False)
