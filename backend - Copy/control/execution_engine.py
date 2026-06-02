"""
TradeOS v6 — Phase 3: Execution Engine
Places orders via Kite Connect after all gates pass.
NEVER called directly — only via telegram_bot.py approval flow.

Gates before any order:
  1. Autonomy phase must be >= 3
  2. Kill switch must be OFF
  3. All portfolio risk checks must pass
  4. Human approval received via Telegram
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, get_config, get_config_int, today_ist, check_kill_switch, logger
from control.risk_manager import check_portfolio_risk, calculate_position_size
from kite.kite_client import get_kite


def place_order(signal: dict, approved_by: str = "TELEGRAM") -> dict:
    """
    Place a real order via Kite. All gates must pass.
    Returns: {success, order_id, error}
    """
    check_kill_switch()

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

    # Place order via Kite
    try:
        kite = get_kite()
        from kiteconnect import KiteConnect
        order_id = kite.place_order(
            variety         = kite.VARIETY_REGULAR,
            exchange        = kite.EXCHANGE_NSE,
            tradingsymbol   = sym,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity        = sizing.quantity,
            product         = kite.PRODUCT_CNC,          # delivery
            order_type      = kite.ORDER_TYPE_MARKET,
            tag             = f"tradeos_{signal.get('strategy','')[:10]}",
        )

        # Log to signal_log
        sb = get_supabase()
        sb.table("signal_log").update({
            "outcome": "EXECUTED",
            "outcome_date": today_ist().isoformat(),
        }).eq("date", today_ist().isoformat()).eq("symbol", sym).execute()

        # Log the execution
        sb.table("open_positions").upsert({
            "symbol":        sym,
            "company_name":  signal.get("company_name", ""),
            "sector":        sector,
            "strategy":      signal.get("strategy", ""),
            "entry_date":    today_ist().isoformat(),
            "entry_price":   entry_price,
            "actual_qty":    sizing.quantity,
            "invested_value": sizing.invested_value,
            "current_price": entry_price,
            "current_value": sizing.invested_value,
            "active_sl":     sizing.stop_loss,
            "initial_sl_atr": sizing.stop_loss,
            "status":        "ACTIVE",
        }, on_conflict="symbol").execute()

        logger.success(f"ORDER PLACED: {sym} | Qty:{sizing.quantity} | ₹{sizing.invested_value:.0f} | OrderID:{order_id}")
        return {
            "success":  True,
            "order_id": order_id,
            "symbol":   sym,
            "qty":      sizing.quantity,
            "invested": sizing.invested_value,
            "stop":     sizing.stop_loss,
        }
    except Exception as e:
        logger.error(f"Order placement FAILED for {sym}: {e}")
        return {"success": False, "error": str(e)}


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
