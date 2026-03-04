"""
TradeOS v6 — Phase 1: Shadow Trade Logger
Logs what the system WOULD have done as paper trades.
Used to validate signal quality before Phase 3 real execution.
Runs alongside live trading — never touches real orders.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, check_kill_switch, logger

# Create shadow_trades table if not exists (run in SQL editor too)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    id              BIGSERIAL   PRIMARY KEY,
    signal_date     DATE        NOT NULL,
    symbol          TEXT        NOT NULL,
    strategy        TEXT,
    signal_type     TEXT,
    paper_entry     NUMERIC,
    paper_stop      NUMERIC,
    paper_target_1  NUMERIC,
    paper_target_2  NUMERIC,
    paper_qty       INTEGER,
    paper_invested  NUMERIC,
    ai_conviction   TEXT,
    score           NUMERIC,
    regime          TEXT,
    -- Outcome tracking
    paper_exit      NUMERIC,
    paper_exit_date DATE,
    paper_pnl       NUMERIC,
    paper_pnl_pct   NUMERIC,
    outcome         TEXT,       -- WIN, LOSS, OPEN
    status          TEXT DEFAULT 'OPEN',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

def log_signal_as_shadow_trade(signal: dict) -> bool:
    """
    Log a BUY_CANDIDATE signal as a paper trade entry.
    Called by generate_signals.py when signal fires.
    """
    if signal.get("signal_type") != "BUY_CANDIDATE":
        return False
    if signal.get("regime_blocked"):
        return False

    sb = get_supabase()
    sym         = signal.get("symbol", "")
    entry_price = float(signal.get("current_price") or signal.get("entry_zone_low") or 0)
    if not entry_price:
        return False

    atr_pct     = float(signal.get("atr_pct") or 2.0)
    atr         = entry_price * atr_pct / 100
    stop_price  = round(entry_price - 2 * atr, 2)
    target_1    = round(entry_price + 3 * atr, 2)
    target_2    = round(entry_price + 5 * atr, 2)

    # Simple position size: ₹15,000 per shadow trade
    paper_invested = 15000.0
    paper_qty = max(1, int(paper_invested / entry_price))

    row = {
        "signal_date":   today_ist().isoformat(),
        "symbol":        sym,
        "strategy":      signal.get("strategy", ""),
        "signal_type":   signal.get("signal_type", ""),
        "paper_entry":   entry_price,
        "paper_stop":    stop_price,
        "paper_target_1": target_1,
        "paper_target_2": target_2,
        "paper_qty":     paper_qty,
        "paper_invested": paper_qty * entry_price,
        "ai_conviction": signal.get("ai_conviction", ""),
        "score":         signal.get("score"),
        "regime":        signal.get("regime", ""),
        "status":        "OPEN",
    }

    try:
        sb.table("shadow_trades").insert(row).execute()
        logger.debug(f"Shadow trade logged: {sym} @ ₹{entry_price}")
        return True
    except Exception as e:
        logger.warning(f"Shadow trade log failed for {sym}: {e}")
        return False

def update_shadow_outcomes():
    """
    Update open shadow trades with current prices and mark outcomes.
    Run daily as part of pipeline to track paper trade P&L.
    """
    check_kill_switch()
    sb = get_supabase()
    today = today_ist()

    open_trades = sb.table("shadow_trades").select("*").eq("status", "OPEN").execute().data
    if not open_trades:
        return {"updated": 0}

    # Get today's prices from stock_data_daily
    syms = list({t["symbol"] for t in open_trades})
    prices = {}
    try:
        rows = sb.table("stock_data_daily").select("symbol,close") \
            .eq("date", today.isoformat()).in_("symbol", syms).execute().data
        prices = {r["symbol"]: float(r["close"] or 0) for r in rows}
    except Exception:
        pass

    updated = 0
    for trade in open_trades:
        sym         = trade["symbol"]
        curr_price  = prices.get(sym)
        if not curr_price:
            continue

        entry  = float(trade["paper_entry"] or 0)
        stop   = float(trade["paper_stop"] or 0)
        target = float(trade["paper_target_1"] or 0)
        qty    = int(trade["paper_qty"] or 1)
        pnl    = (curr_price - entry) * qty
        pnl_pct = (curr_price - entry) / entry * 100 if entry else 0

        # Determine outcome
        outcome = "OPEN"
        status  = "OPEN"
        exit_price = None
        exit_date  = None

        if curr_price <= stop:
            outcome    = "LOSS"
            status     = "CLOSED"
            exit_price = stop
            exit_date  = today.isoformat()
        elif curr_price >= target:
            outcome    = "WIN"
            status     = "CLOSED"
            exit_price = target
            exit_date  = today.isoformat()

        update = {
            "paper_exit":      exit_price,
            "paper_exit_date": exit_date,
            "paper_pnl":       round(pnl, 2),
            "paper_pnl_pct":   round(pnl_pct, 2),
            "outcome":         outcome,
            "status":          status,
        }
        sb.table("shadow_trades").update(update).eq("id", trade["id"]).execute()
        updated += 1

    logger.info(f"Shadow trades updated: {updated}")
    return {"updated": updated}

def get_shadow_performance() -> dict:
    """Summary statistics for shadow trades (used in frontend)."""
    sb = get_supabase()
    trades = sb.table("shadow_trades").select("*").neq("status", "OPEN").execute().data
    if not trades:
        return {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_r": 0}

    wins = [t for t in trades if t.get("outcome") == "WIN"]
    total_pnl = sum(float(t.get("paper_pnl") or 0) for t in trades)
    avg_pnl_pct = sum(float(t.get("paper_pnl_pct") or 0) for t in trades) / len(trades)

    return {
        "trades":    len(trades),
        "wins":      len(wins),
        "win_rate":  round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_r":     round(avg_pnl_pct, 2),
    }
