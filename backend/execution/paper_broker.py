"""
Paper execution — real decisions, simulated fills.

WHY THIS IS NOT A TOY
---------------------
The intraday framework has never placed a trade, and the swing framework has one
signal-attributed outcome out of seventy closed positions. Both are now allowed
to sell real holdings automatically. Paper mode is how the seven intraday
engines and the runner logic earn that permission on evidence rather than on
having compiled.

The critical property is that ONLY the fill is simulated. The universe scan, the
engines, the index gate, the news gate, the cost model, the exit ladder, the
runner assessment and the alerts are all the identical code path. A paper result
is therefore a real statement about the decision logic — which a separate
backtest harness could never be, because a backtest is a second implementation
and would drift from the thing it is meant to measure.

PESSIMISTIC BY CONSTRUCTION
---------------------------
Every simplification is deliberately biased AGAINST the system:

    fills          at the worse side of the spread, never at the mid
    slippage       applied on both legs, from the same cost model as live
    charges        full round trip, not ignored
    limit orders   fill only when price actually TRADES THROUGH the limit,
                   not when it merely touches — a touch is not a fill, and
                   assuming otherwise is the single most common way paper
                   results beat live ones
    partial fills  not simulated; the whole order fills or none of it, which
                   is optimistic, so it is stated here rather than hidden

A paper system that flatters itself is worse than no paper system, because it
produces confidence rather than information.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, get_supabase, cfg_float, cfg_int, today_ist


@dataclass
class PaperFill:
    ok: bool
    order_id: str | None
    fill_price: float | None
    quantity: int
    charges: float
    message: str


def _slippage_pct() -> float:
    return cfg_float("cost_slippage_bps", 5.0) / 10000.0


def simulate_fill(symbol: str, side: str, qty: int, order_type: str,
                  limit_price: float | None, ltp: float) -> PaperFill:
    """
    Would this order have filled, and at what price?

    LIMIT orders require price to trade THROUGH the limit, not merely touch it.
    On a buy limit that means the market must be at or below the limit; on a
    sell limit, at or above. Treating a touch as a fill is exactly how paper
    equity curves come out ahead of live ones.
    """
    if qty <= 0 or not ltp or ltp <= 0:
        return PaperFill(False, None, None, 0, 0.0, "no quantity or price")

    slip = _slippage_pct()
    if order_type == "MARKET":
        # Pay the spread in the direction that hurts.
        fill = ltp * (1 + slip) if side == "BUY" else ltp * (1 - slip)
    else:
        if limit_price is None:
            return PaperFill(False, None, None, 0, 0.0, "limit order without a price")
        if side == "BUY" and ltp > limit_price:
            return PaperFill(False, None, None, 0, 0.0,
                             f"not filled — market {ltp:.2f} above the {limit_price:.2f} limit")
        if side == "SELL" and ltp < limit_price:
            return PaperFill(False, None, None, 0, 0.0,
                             f"not filled — market {ltp:.2f} below the {limit_price:.2f} limit")
        # Filled at the limit, never better. Price improvement happens live but
        # assuming it here would be free money the simulation invented.
        fill = limit_price

    try:
        from intraday.cost_model import round_trip
        # Half a round trip — this is one leg.
        charges = round(round_trip(fill, qty).total / 2.0, 2)
    except Exception:
        charges = 0.0

    oid = f"PAPER-{datetime.now(IST):%Y%m%d%H%M%S}-{symbol[:6]}"
    return PaperFill(True, oid, round(fill, 2), qty, charges,
                     f"paper fill {qty} @ {fill:.2f} (charges ₹{charges:.2f})")


def place(symbol: str, side: str, qty: int, order_type: str,
          limit_price: float | None, ltp: float, framework: str = "SWING",
          reason: str = "", sb=None) -> PaperFill:
    """
    Record a simulated order and its fill.

    Writes to the same intraday_broker_log as live orders, tagged PAPER, so one
    query answers "what did the system do today" regardless of mode — and so a
    mode mix-up is visible rather than hidden in a separate table nobody reads.
    """
    sb = sb or get_supabase()
    f = simulate_fill(symbol, side, qty, order_type, limit_price, ltp)

    try:
        sb.table("intraday_broker_log").insert({
            "ts": datetime.now(IST).isoformat(),
            "symbol": symbol,
            "channel": "PAPER",
            "action": "FILLED" if f.ok else "NOT_FILLED",
            "side": side,
            "ref_id": f.order_id,
            "price": f.fill_price,
            "quantity": qty if f.ok else 0,
            "detail": f"[{framework}] {reason} | {f.message}",
        }).execute()
    except Exception as e:
        logger.debug(f"  paper log write failed: {e}")

    if f.ok:
        logger.info(f"  📄 PAPER {side} {qty} {symbol} @ {f.fill_price:.2f} "
                    f"— {reason[:60]}")
    else:
        logger.info(f"  📄 PAPER {side} {symbol} NOT FILLED — {f.message}")
    return f


def capacity(framework: str = "INTRADAY", sb=None) -> tuple[bool, str, float]:
    """
    Is there room for another paper position?

    Returns (allowed, reason, cash_left). Paper capital is notional and separate
    from the real account, so a simulation is not constrained by cash that is
    already deployed live — otherwise the paper run would silently stop taking
    setups for reasons that have nothing to do with whether they were good.

    Concentration is still enforced. A simulation that opens forty positions
    tests nothing about a system that can hold four, and its results would not
    transfer to the account it is meant to inform.
    """
    sb = sb or get_supabase()
    cap = cfg_float("paper_starting_capital", 100000.0)
    max_open = cfg_int("paper_max_open_positions", 5)
    try:
        rows = (sb.table("open_positions")
                  .select("symbol,invested_value,framework")
                  .eq("mode", "PAPER").eq("status", "ACTIVE").execute().data or [])
    except Exception as e:
        return False, f"could not read paper book: {e}", 0.0

    mine = [r for r in rows if (r.get("framework") or "").upper() == framework.upper()]
    deployed = sum(float(r.get("invested_value") or 0) for r in rows)
    left = cap - deployed

    if len(mine) >= max_open:
        return False, f"{len(mine)} paper {framework} positions already open (cap {max_open})", left
    if left <= 0:
        return False, f"paper capital exhausted — ₹{deployed:,.0f} of ₹{cap:,.0f} deployed", left
    return True, f"₹{left:,.0f} of paper capital free", left


def close_position(symbol: str, exit_price: float, reason: str, detail: str,
                   sb=None) -> bool:
    """
    Close a paper position through the SAME path a live one uses.

    Delegates to control.position_lifecycle.close_position, which computes the
    realised R multiple, hold days, excursions and attribution. Reimplementing
    any of that here would give paper trades a different outcome record from
    live ones and make the two incomparable — which would defeat the only reason
    the simulation exists.
    """
    sb = sb or get_supabase()
    try:
        rows = (sb.table("open_positions").select("*")
                  .eq("symbol", symbol).eq("mode", "PAPER").execute().data or [])
        if not rows:
            return False
        from control.position_lifecycle import close_position as _close
        ok = _close(sb, rows[0], float(exit_price), reason, detail,
                    today_ist().isoformat(), source="paper")
        if ok:
            logger.success(f"  📄 PAPER CLOSED {symbol} @ {exit_price:.2f} — {reason}")
        return ok
    except Exception as e:
        logger.warning(f"  paper close failed for {symbol}: {e}")
        return False


def open_position(symbol: str, qty: int, fill_price: float, setup: dict,
                  framework: str = "INTRADAY", sb=None) -> bool:
    """
    Create a PAPER position that the normal exit engine will manage.

    Deliberately written to open_positions with mode='PAPER' rather than to a
    separate table. Everything downstream — the exit ladder, the runner
    assessment, MFE/MAE tracking, the dashboard, closed_positions and the
    learning loop — then works on paper trades with no special case, which is
    the only way the simulation tests the real system rather than a copy of it.
    """
    sb = sb or get_supabase()
    try:
        sb.table("open_positions").upsert({
            "symbol": symbol,
            "mode": "PAPER",
            "framework": framework,
            "product": "CNC",
            "status": "ACTIVE",
            "entry_date": today_ist().isoformat(),
            "entry_price": fill_price,
            "actual_qty": qty,
            "current_qty": qty,
            "original_qty": qty,
            "invested_value": round(fill_price * qty, 2),
            "current_price": fill_price,
            "high_water_mark": fill_price,
            "planned_stop": setup.get("stop"),
            "planned_target": setup.get("target"),
            "target_price": setup.get("target"),
            "active_sl": setup.get("stop"),
            "sl_type": "PAPER_SETUP",
            # The price whose loss kills the thesis. Stored so the exit monitor
            # can check it mechanically rather than parsing the prose the engine
            # wrote for the alert.
            "invalidation_level": setup.get("invalidation_level"),
            "invalidation_note": setup.get("invalidation_note"),
            "strategy": setup.get("strategy"),
            "intraday_strategy": setup.get("strategy") if framework == "INTRADAY" else None,
            "entry_signal_type": setup.get("strategy"),
            "source": "paper",
            "synced_at": datetime.now(IST).isoformat(),
        }, on_conflict="symbol").execute()
        logger.success(f"  📄 PAPER POSITION opened {symbol} {qty} @ {fill_price:.2f} "
                       f"[{framework}/{setup.get('strategy')}]")
        return True
    except Exception as e:
        logger.warning(f"  paper position open failed for {symbol}: {e}")
        return False
