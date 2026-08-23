"""
Intraday configuration — market hours, phase gates, tunables.

Every switch defaults to the SAFE position. Turning this subsystem on should
never be something that happens by accident, by inheriting a config row, or by
a default flipping during an upgrade.
"""

from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import IST, cfg_bool, cfg_float, cfg_int, get_supabase


# ── Market hours (IST) ──────────────────────────────────────────────────────
# The daemon runs only while the market is open, plus a short pre-open warm-up
# to load state and a short post-close tail to catch the closing print.
#
# NOT 24/7, and deliberately so: NSE equities trade 09:15-15:30 Mon-Fri, which
# is 31 hours of the 168 in a week. Outside them there are no ticks to react
# to, and running anyway would burn a broker session, add API load, and
# accumulate rows that describe nothing.
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
WARMUP_FROM  = dtime(9,  0)
COOLDOWN_TO  = dtime(15, 40)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_session(now: datetime | None = None) -> bool:
    """True while the daemon should be awake — includes warm-up and cool-down."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    return WARMUP_FROM <= now.time() <= COOLDOWN_TO


def is_market_open(now: datetime | None = None) -> bool:
    """True only during actual trading — gates anything that places an order."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def minutes_since_open(now: datetime | None = None) -> float:
    """
    Elapsed session minutes, clamped to [0, session length].

    Used to scale a 20-day average volume down to "expected volume BY NOW" —
    at 09:20 a stock is not behind because it has traded only 5 minutes'
    worth of its usual day. Before the open this is 0 (no live signal yet,
    not a negative one); after the close it holds at the full session length
    rather than growing, so a post-close read cannot manufacture a rising
    RVOL out of elapsed wall-clock time.
    """
    now = now or now_ist()
    open_dt = datetime.combine(now.date(), MARKET_OPEN, tzinfo=now.tzinfo)
    close_dt = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=now.tzinfo)
    session_len = (close_dt - open_dt).total_seconds() / 60.0
    elapsed = (now - open_dt).total_seconds() / 60.0
    return max(0.0, min(elapsed, session_len))


def is_holiday(sb=None) -> bool:
    try:
        sb = sb or get_supabase()
        today = now_ist().date().isoformat()
        rows = sb.table("nse_holidays").select("date").eq("date", today).execute().data
        return bool(rows)
    except Exception:
        # An unreachable holiday table must not silently stop the daemon on a
        # normal trading day. Failing open here is safe: the worst case is a
        # loop that finds no ticks on a holiday and does nothing.
        return False


# ── Phase gates ─────────────────────────────────────────────────────────────
# 2.0  monitor + notify only. No broker writes of any kind.
# 2.5  + GTT stops placed and maintained at the broker.
# 3.0  + orders placed automatically.
#
# Read live on every use rather than cached at import, so the Control Room can
# stop the system mid-session without a restart.
def autonomy_phase() -> float:
    return cfg_float("intraday_autonomy_phase", 2.0)


def gtt_enabled() -> bool:
    """Phase 2.5 — broker-side stops."""
    return autonomy_phase() >= 2.5 and cfg_bool("intraday_gtt_enabled", False)


def orders_enabled() -> bool:
    """
    Phase 3 — automatic order placement.

    THREE independent switches must all be true. That is not redundancy: each
    one fails in a different way. The phase is a deliberate promotion; the
    boolean is a fast off-switch that does not require re-reasoning about the
    phase; and DRY_RUN is honoured so the standard "run it safely" idiom used
    everywhere else in this codebase keeps working here, where it matters most.
    """
    from config import DRY_RUN
    return (autonomy_phase() >= 3.0
            and cfg_bool("intraday_orders_enabled", False)
            and not DRY_RUN)


# ── Risk rails (Phase 3) ────────────────────────────────────────────────────
def max_orders_per_day() -> int:
    return cfg_int("intraday_max_orders_per_day", 5)


def max_order_value() -> float:
    """Hard rupee ceiling per order, independent of what sizing computes."""
    return cfg_float("intraday_max_order_value", 10000.0)


def max_notional_per_day() -> float:
    return cfg_float("intraday_max_notional_per_day", 25000.0)


# ── Loop tuning ─────────────────────────────────────────────────────────────
def eval_interval_s() -> int:
    """
    How often the decision loop runs.

    Ticks arrive continuously over the websocket, but EVALUATING on every tick
    would be both wasteful and wrong: a stop is not more likely to be breached
    because five ticks arrived in a second, and re-deciding at tick rate makes
    the notifier's de-duplication the only thing standing between you and
    hundreds of messages. Ticks update state; a timer decides.
    """
    return cfg_int("intraday_eval_interval_s", 15)


def position_guard_interval_s() -> int:
    """
    How often OPEN POSITIONS are re-checked against the exit ladder.

    Split out of `eval_interval_s()` on 19-Aug-2026. Finding a new setup and
    defending an open one were sharing a timer sized for the expensive one:
    the full scan costs ~120 symbols x 9 engines and writes detection rows,
    while the exit check reads a handful of in-memory positions and writes
    only when a rung actually fires. See `engine.guard_positions()` for why
    the two do not belong on the same clock.

    Returning a value >= eval_interval_s() disables the fast lane exactly —
    the guard then never comes due between full cycles, which already run it.
    That is the rollback, and it is the reason this is a floor rather than an
    independent timer.
    """
    return cfg_int("intraday_position_guard_interval_s", 3)


def live_requalify_interval_s() -> int:
    """
    How often the live universe re-qualification check runs — Stage D2,
    23-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D).

    Separate from `eval_interval_s()`'s 300s bench rebuild for the same
    reason `position_guard_interval_s()` is separate from it: the bench
    rebuild reads a fresh 1,800+-row `stock_data_daily` scan and is
    genuinely expensive to run often; this check is a handful of REST
    quote calls for a small, pre-filtered candidate list (names that
    qualify on price/liquidity/delivery but missed only the ATR floor
    yesterday) and is cheap enough to run several times a minute.

    Returning a value >= eval_interval_s() disables the fast lane exactly,
    same rollback shape as position_guard_interval_s().
    """
    return cfg_int("intraday_live_requalify_interval_s", 45)


def gtt_sync_interval_s() -> int:
    """How often GTT triggers are reconciled against the intended stop."""
    return cfg_int("intraday_gtt_sync_interval_s", 300)


def poll_interval_s() -> int:
    """Quote polling cadence when the websocket is unavailable."""
    return cfg_int("intraday_poll_interval_s", 30)
