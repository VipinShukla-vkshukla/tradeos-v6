"""
TradeOS v6 — Phase 3: Risk Manager
Position sizing using ATR-based method.
Portfolio risk checks before any new entry.
"""
import sys
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, get_config_float, get_config_int, TOTAL_CAPITAL, logger


@dataclass
class PositionSizing:
    symbol:         str
    entry_price:    float
    stop_loss:      float
    risk_per_trade: float       # ₹ amount at risk
    quantity:       int
    invested_value: float
    position_pct:   float       # % of total capital
    r_multiple:     float
    sizing_method:  str


def calculate_position_size(
    symbol: str,
    entry_price: float,
    atr: float,
    atr_multiplier: float = 2.0,
    max_risk_pct: float = 1.5,
) -> PositionSizing:
    """
    ATR-based position sizing.
    Stop = entry - (atr_multiplier × ATR)
    Qty = (Capital × max_risk_pct%) / (entry - stop)
    """
    capital = TOTAL_CAPITAL
    stop_loss   = round(entry_price - (atr * atr_multiplier), 2)
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        risk_per_share = entry_price * 0.03   # fallback: 3% stop

    max_risk_inr = capital * (max_risk_pct / 100)
    qty          = int(max_risk_inr / risk_per_share)
    qty          = max(1, qty)
    invested     = round(entry_price * qty, 2)
    pos_pct      = round(invested / capital * 100, 2)

    # Cap: no single position > 15% of capital
    if pos_pct > 15:
        qty      = int((capital * 0.15) / entry_price)
        invested = round(entry_price * qty, 2)
        pos_pct  = round(invested / capital * 100, 2)

    return PositionSizing(
        symbol         = symbol,
        entry_price    = entry_price,
        stop_loss      = stop_loss,
        risk_per_trade = round(risk_per_share * qty, 2),
        quantity       = qty,
        invested_value = invested,
        position_pct   = pos_pct,
        r_multiple     = round(atr_multiplier, 2),
        sizing_method  = "ATR",
    )


def check_portfolio_risk(new_symbol: str, new_sector: str) -> dict:
    """
    Run all portfolio risk checks before allowing a new position.
    Returns: {eligible: bool, reason: str, checks: dict}
    """
    sb = get_supabase()

    # Load current open positions
    positions = sb.table("open_positions").select("symbol,sector,invested_value,current_value").execute().data
    regime    = sb.table("market_regime").select("regime").order("date", desc=True).limit(1).execute().data
    regime_str = regime[0]["regime"] if regime else "NEUTRAL"

    # Get config
    suffix_map = {"RISK ON": "risk_on", "NEUTRAL": "neutral", "RISK OFF": "risk_off"}
    suffix     = suffix_map.get(regime_str, "neutral")
    max_pos    = get_config_int(f"max_positions_{suffix}", 7)
    max_sector_pct = get_config_float("max_sector_concentration", 25.0)
    block_risk_off = sb.table("system_config").select("value").eq("key", "block_buys_risk_off").execute().data
    block_risk_off = (block_risk_off[0]["value"].lower() == "true") if block_risk_off else False

    checks = {}

    # 1. Max positions
    current_count = len(positions)
    checks["max_positions"] = {
        "pass":    current_count < max_pos,
        "current": current_count,
        "limit":   max_pos,
    }

    # 2. Sector concentration
    sector_invested = sum(
        float(p.get("invested_value") or 0)
        for p in positions
        if p.get("sector", "").lower() == (new_sector or "").lower()
    )
    total_invested  = sum(float(p.get("invested_value") or 0) for p in positions)
    sector_pct      = (sector_invested / TOTAL_CAPITAL * 100) if TOTAL_CAPITAL > 0 else 0
    checks["sector_concentration"] = {
        "pass":        sector_pct + (TOTAL_CAPITAL * 0.10 / TOTAL_CAPITAL * 100) <= max_sector_pct,
        "sector":      new_sector,
        "current_pct": round(sector_pct, 1),
        "limit_pct":   max_sector_pct,
    }

    # 3. Regime gate
    checks["regime"] = {
        "pass":    not (regime_str == "RISK OFF" and block_risk_off),
        "regime":  regime_str,
        "blocked": regime_str == "RISK OFF" and block_risk_off,
    }

    # 4. ASM/GSM check
    safety = sb.table("safety_lists").select("list_type,stage").eq("symbol", new_symbol).execute().data
    asm_blocked = any(r["list_type"] in ("ASM", "GSM") for r in safety)
    checks["safety"] = {
        "pass":     not asm_blocked,
        "asm_gsm":  asm_blocked,
        "fo_ban":   any(r["list_type"] == "FO_BAN" for r in safety),
    }

    all_pass = all(c["pass"] for c in checks.values())
    reason   = "All checks passed"
    if not all_pass:
        failed = [k for k, v in checks.items() if not v["pass"]]
        reason = f"Failed: {', '.join(failed)}"

    return {"eligible": all_pass, "reason": reason, "checks": checks, "regime": regime_str}
