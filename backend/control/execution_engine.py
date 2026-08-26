"""
TradeOS v7 — Phase 3: Execution Engine
Places orders via Kite Connect after all gates pass.
NEVER called directly — only via telegram_bot.py approval flow.

Gates before any order:
  1. Autonomy phase must be >= 3
  2. Kill switch must be OFF
  3. All portfolio risk checks must pass
  4. Human approval received via Telegram
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# IMPORT FIX 2026-07. This module previously failed at import time:
#   from config import get_config, get_config_int, check_kill_switch
# None of those three names exist in config.py — the real ones are cfg,
# cfg_int and is_kill_switch_active. It also imported `kite.kite_client`,
# which did not exist in the repo at all. So execution_engine could never be
# loaded, and telegram_bot's `from control.execution_engine import place_order`
# (control/telegram_bot.py:166) raised on every approval attempt.
from config import (get_supabase, cfg_int, cfg_bool, today_ist, IST,
                    is_kill_switch_active, logger)
from control.risk_manager import check_portfolio_risk, calculate_position_size
from kite.kite_client import get_kite


def check_kill_switch():
    """Raise if the master kill switch is engaged. Order placement must abort,
    not merely log — this is the last gate before real money moves."""
    if is_kill_switch_active():
        raise RuntimeError("KILL SWITCH ACTIVE — order placement refused")


def get_config_int(key: str, default: int = 0) -> int:
    """Back-compat alias for cfg_int, kept so the call sites below read the
    same as they always have."""
    return cfg_int(key, default)


def place_order(signal: dict, approved_by: str = "TELEGRAM") -> dict:
    """
    Place a real swing order on human Telegram approval — through the SAME
    order_manager.place()/preflight() and _upsert_position() path every
    other swing entry uses. Returns: {success, order_id, error}

    REBUILT 26-Aug-2026 — Phase 2a of the swing framework evolution
    blueprint (docs/PHASE4_RED_TEAM.md's "C2 — 'One allocator' is false"
    finding, third of the three independent swing paths it names; the other
    two — the daemon's own _maybe_enter_swing and control/
    candidate_monitor.py — are closed by Phases 1 and 2b of the same
    blueprint). The prior version called `kite.place_order()` directly:
    no `preflight()` (no per-order cap, no daily count/notional cap, no
    combined-account guard, no broker-cash check, no duplicate-order
    window), no `swing_auto_entry`/`swing_live_auto_entry` check (the
    two-switch live-money gate every other swing entry respects), and wrote
    an `open_positions` row missing `product`/`framework`/`mode` — a real
    corruption risk given the table is keyed on (symbol, product) since
    migration 028. Sizing (`check_portfolio_risk`/`calculate_position_size`)
    is unchanged; only order placement and position-recording now reuse the
    daemon's own proven functions instead of a second implementation of
    both.
    """
    check_kill_switch()

    # THE TWO-SWITCH GATE — every other swing entry in this system respects
    # both of these before spending real money; a human-approved order is
    # not exempt from that rule just because a person clicked the button.
    if not cfg_bool("swing_auto_entry", False):
        return {"success": False, "error": "swing_auto_entry is off"}
    if not cfg_bool("swing_live_auto_entry", False):
        return {"success": False,
                "error": "swing_live_auto_entry is off — the second of the "
                         "two required live-money switches"}

    # Gate 1: autonomy phase
    phase = get_config_int("autonomy_phase", 0)
    if phase < 3:
        return {"success": False, "error": f"Phase {phase} — execution not enabled until Phase 3"}

    sym          = signal.get("symbol", "")
    entry_price  = float(signal.get("entry_price") or signal.get("current_price") or 0)
    atr_pct      = float(signal.get("atr_pct") or 2.0)
    atr          = entry_price * atr_pct / 100
    sector       = signal.get("sector", "")

    # Gate 2: portfolio risk checks
    risk = check_portfolio_risk(sym, sector)
    if not risk["eligible"]:
        return {"success": False, "error": f"Risk check failed: {risk['reason']}"}

    # Gate 3: size the position
    sizing = calculate_position_size(sym, entry_price, atr)
    if sizing.quantity <= 0:
        return {"success": False, "error": "Position size calculated as 0"}

    sb = get_supabase()

    # PLACE THROUGH THE SAME PATH EVERY OTHER SWING ENTRY USES. This is
    # what makes the daily order-count/notional caps, the combined-account
    # guard, the broker-cash check and the duplicate-order window apply
    # here too — none of them can be bypassed by approving from Telegram.
    from execution.order_manager import OrderRequest, place
    res = place(OrderRequest(sym, "BUY", sizing.quantity, "MARKET",
                             reason=f"TELEGRAM_APPROVED: {str(signal.get('strategy',''))[:80]}"),
               sb, notifier=None, framework="SWING")
    if not (res and res.ok):
        return {"success": False,
                "error": res.message if res else "order_manager.place() returned no result"}

    # Log to signal_log — unchanged.
    sb.table("signal_log").update({
        "outcome": "EXECUTED",
        "outcome_date": today_ist().isoformat(),
    }).eq("date", today_ist().isoformat()).eq("symbol", sym).execute()

    # WRITE THE SAME SHAPE _maybe_enter_swing WRITES (intraday/engine.py) —
    # status PENDING_FILL, not ACTIVE. res.ok only means Kite ACCEPTED the
    # order, not that it filled; the daemon's own _resolve_pending_fills()
    # already polls every PENDING_FILL row each cycle regardless of which
    # path submitted it, so this confirms/promotes on the next daemon cycle
    # with no new machinery needed here.
    from control.position_lifecycle import _upsert_position
    _upsert_position(sb, {
        "symbol": sym, "mode": "LIVE", "framework": "SWING",
        "product": "CNC", "status": "PENDING_FILL",
        "entry_order_id": str(res.order_id),
        "entry_date": today_ist().isoformat(), "entry_price": entry_price,
        "actual_qty": sizing.quantity, "current_qty": sizing.quantity,
        "original_qty": sizing.quantity,
        "invested_value": round(entry_price * sizing.quantity, 2),
        "current_price": entry_price, "high_water_mark": entry_price,
        "planned_stop": sizing.stop_loss, "active_sl": sizing.stop_loss,
        "planned_target": signal.get("planned_target"),
        "target_price": signal.get("planned_target"),
        "sl_type": "TELEGRAM_APPROVED", "strategy": signal.get("strategy"),
        "signal_id": signal.get("id"), "signal_date": signal.get("date"),
        "sector": sector, "source": "telegram_approved",
        "entry_rationale": f"Telegram-approved by {approved_by}",
        "synced_at": datetime.now(IST).isoformat(),
    })

    logger.success(f"ORDER PLACED (Telegram): {sym} | Qty:{sizing.quantity} | "
                   f"₹{sizing.invested_value:.0f} | OrderID:{res.order_id}")
    return {
        "success":  True,
        "order_id": res.order_id,
        "symbol":   sym,
        "qty":      sizing.quantity,
        "invested": sizing.invested_value,
        "stop":     sizing.stop_loss,
    }


def place_exit_order(symbol: str, quantity: int, reason: str = "SIGNAL") -> dict:
    """Place a market sell order to exit a position."""
    check_kill_switch()
    phase = get_config_int("autonomy_phase", 0)
    if phase < 3:
        return {"success": False, "error": "Execution not enabled"}
    try:
        kite = get_kite()
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = kite.EXCHANGE_NSE,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_SELL,
            quantity         = quantity,
            product          = kite.PRODUCT_CNC,
            order_type       = kite.ORDER_TYPE_MARKET,
            tag              = f"tradeos_exit",
        )
        logger.success(f"EXIT ORDER: {symbol} | Qty:{quantity} | Reason:{reason} | OrderID:{order_id}")
        return {"success": True, "order_id": order_id, "symbol": symbol, "qty": quantity}
    except Exception as e:
        logger.error(f"Exit order FAILED for {symbol}: {e}")
        return {"success": False, "error": str(e)}
