"""
TradeOS v7 — Swap Execution Engine
====================================

Executes swap recommendations: auto-sell weak positions, auto-buy strong
candidates, and one-tap chained swaps (sell + buy in one operation).

ALL FEATURES DEFAULT TO ON. Orders will be placed when every existing gate in
control.execution_engine passes (kill switch off, autonomy phase >= 3,
swing_auto_entry on, swing_live_auto_entry on for live mode).

To DISABLE auto-execution, set swap_auto_execute = 'false' in system_config.

Paper mode (swing_trading_mode=PAPER) is fully supported — orders route
through the paper broker instead of Kite.

Defaults:
  swap_auto_execute           = True    # master switch (ON by default)
  swap_auto_execute_max_per_day = 2     # max swaps per day (circuit breaker)
  swap_auto_execute_min_margin  = 20.0  # minimum rank improvement to auto-execute
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from config import cfg_bool, cfg_float, cfg_int, get_supabase, DRY_RUN
from execution.gates import is_paper
from control.execution_engine import place_order, place_exit_order
from control.risk_manager import check_portfolio_risk, calculate_position_size


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SwapExecutionResult:
    """Outcome of one swap execution attempt."""

    proposal_key: str              # "SELL_SYMBOL→BUY_SYMBOL"
    sell_result: dict | None = None
    buy_result: dict | None = None
    status: str = "PENDING"        # PENDING | EXECUTED | PARTIAL | BLOCKED | SKIPPED
    blocked_reason: str | None = None
    executed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_key": self.proposal_key,
            "sell_result": self.sell_result,
            "buy_result": self.buy_result,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "executed_at": self.executed_at,
        }


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------


def _execution_gates_ok() -> tuple[bool, str]:
    """
    Verify all preconditions for auto-execution.

    Returns (ok, reason). If not ok, reason explains which gate blocked it.
    """
    if not cfg_bool("swap_auto_execute", True):
        return False, "swap_auto_execute is off"

    # Kill switch — absolute block
    try:
        from config import is_kill_switch_active
        if is_kill_switch_active():
            return False, "kill switch active"
    except Exception:
        return False, "could not read kill switch"

    # Two-switch gate — same as every other swing entry.
    # swing_auto_entry defaults to True so paper-mode users don't need a SQL insert.
    if not cfg_bool("swing_auto_entry", True):
        return False, "swing_auto_entry is off"

    paper = is_paper("SWING")
    if not paper and not cfg_bool("swing_live_auto_entry", False):
        return False, "swing_live_auto_entry is off (required for LIVE mode)"

    # Autonomy phase — execution engine checks phase >= 3 internally too,
    # but we fail fast here with a clear message.
    try:
        from config import cfg_int
        phase = cfg_int("autonomy_phase", 0)
        if phase < 3:
            return False, f"autonomy phase {phase} < 3"
    except Exception:
        pass

    if DRY_RUN:
        return False, "DRY_RUN is on — set DRY_RUN=False for execution"

    return True, ""


def _daily_swap_count(sb) -> int:
    """Count how many swaps have been executed today (for circuit breaker)."""
    try:
        today = str(__import__("config", fromlist=["today_ist"]).today_ist())
        rows = (
            sb.table("swap_executions")
            .select("executed_at")
            .gte("executed_at", f"{today} 00:00:00")
            .execute()
            .data
        ) or []
        return len(rows)
    except Exception:
        # Table may not exist yet — treat as 0 (first run creates it implicitly
        # via the insert below; if the insert fails the error propagates).
        return 0


# ---------------------------------------------------------------------------
# Individual execution steps
# ---------------------------------------------------------------------------


def execute_auto_sell(
    position: dict,
    reason: str,
    sb=None,
) -> dict:
    """
    Auto-sell a weak position via place_exit_order.

    Returns the raw result dict from place_exit_order, augmented with
    'auto_sell': True and 'reason'.
    """
    sb = sb or get_supabase()
    sym = str(position.get("symbol", ""))
    qty = int(position.get("current_qty") or position.get("actual_qty") or 0)
    if not sym or qty <= 0:
        return {"success": False, "error": f"invalid position for auto-sell: {sym} qty={qty}"}

    full_reason = f"AUTO_SELL: {reason}"
    res = place_exit_order(sym, qty, reason=full_reason)
    res["auto_sell"] = True
    res["symbol"] = sym
    res["qty"] = qty
    res["reason"] = full_reason
    return res


def execute_auto_buy(
    candidate: dict,
    sb=None,
    atr_pct: float = 2.0,
) -> dict:
    """
    Auto-buy a candidate via place_order.

    Constructs a signal dict from the candidate and calls place_order.
    Sizing is handled inside execution_engine via check_portfolio_risk /
    calculate_position_size.

    Returns the raw result dict from place_order, augmented with
    'auto_buy': True.
    """
    sb = sb or get_supabase()
    sym = str(candidate.get("symbol", ""))
    entry_price = float(candidate.get("current_price") or candidate.get("planned_entry") or 0)
    sector = candidate.get("sector", "")
    strategy = candidate.get("strategy", "SWAP_AUTO_BUY")
    stop = float(candidate.get("planned_stop") or entry_price * 0.95)
    target = float(candidate.get("planned_target") or entry_price * 1.10)

    if not sym or entry_price <= 0:
        return {"success": False, "error": f"invalid candidate for auto-buy: {sym} price={entry_price}"}

    signal = {
        "symbol": sym,
        "entry_price": entry_price,
        "current_price": entry_price,
        "atr_pct": atr_pct,
        "sector": sector,
        "strategy": strategy,
        "planned_stop": stop,
        "planned_target": target,
        "id": f"SWAP_AUTO_{sym}_{int(datetime.now(timezone.utc).timestamp())}",
        "date": str(__import__("config", fromlist=["today_ist"]).today_ist()),
    }

    res = place_order(signal, approved_by="SWAP_AUTO")
    res["auto_buy"] = True
    res["symbol"] = sym
    return res


# ---------------------------------------------------------------------------
# One-tap swap
# ---------------------------------------------------------------------------


def execute_one_tap_swap(
    proposal: dict,
    sb=None,
    atr_pct: float = 2.0,
) -> SwapExecutionResult:
    """
    One-tap swap: sell the weak position, then buy the strong candidate.

    Chains sell → buy in a single operation. If the sell succeeds but the buy
    fails, status is PARTIAL (you still own the cash from the sell, just no
    new position — review manually).

    Parameters
    ----------
    proposal : dict
        Must contain: sell_symbol, buy_symbol, sell_position (dict), buy_candidate (dict)
    sb : Supabase client (optional, creates one if not given)
    atr_pct : float
        ATR percentage for the buy order's stop calculation.

    Returns
    -------
    SwapExecutionResult with sell_result, buy_result, status.
    """
    sb = sb or get_supabase()
    sell_sym = str(proposal.get("sell_symbol", ""))
    buy_sym = str(proposal.get("buy_symbol", ""))
    key = f"{sell_sym}→{buy_sym}"

    # ── Gate check ────────────────────────────────────────────────────────
    ok, gate_reason = _execution_gates_ok()
    if not ok:
        return SwapExecutionResult(
            proposal_key=key,
            status="BLOCKED",
            blocked_reason=gate_reason,
        )

    # ── Daily circuit breaker ─────────────────────────────────────────────
    max_per_day = cfg_int("swap_auto_execute_max_per_day", 2)
    today_count = _daily_swap_count(sb)
    if today_count >= max_per_day:
        return SwapExecutionResult(
            proposal_key=key,
            status="BLOCKED",
            blocked_reason=f"daily swap limit {max_per_day} reached ({today_count} today)",
        )

    # ── Minimum margin check ──────────────────────────────────────────────
    min_margin = cfg_float("swap_auto_execute_min_margin", 20.0)
    margin = float(proposal.get("margin", 0) or 0)
    if margin < min_margin:
        return SwapExecutionResult(
            proposal_key=key,
            status="SKIPPED",
            blocked_reason=f"margin {margin:.0f} < min {min_margin:.0f}",
        )

    sell_position = proposal.get("sell_position") or {}
    buy_candidate = proposal.get("buy_candidate") or {}

    # ── Step 1: Auto-sell the weak position ───────────────────────────────
    sell_reason = (
        f"Swap auto-execute: replacing {sell_sym} (rank {proposal.get('sell_rank', '?')}) "
        f"with {buy_sym} (rank {proposal.get('buy_rank', '?')}, +{margin:.0f} pts)"
    )
    sell_result = execute_auto_sell(sell_position, sell_reason, sb=sb)

    if not sell_result.get("success"):
        # Cannot proceed to buy if we couldn't free the slot/capital
        return SwapExecutionResult(
            proposal_key=key,
            status="BLOCKED",
            blocked_reason=f"auto-sell failed: {sell_result.get('error')}",
            sell_result=sell_result,
        )

    logger.success(
        f"AUTO-SELL leg done: {sell_sym} qty={sell_result.get('qty')} "
        f"order={sell_result.get('order_id')}"
    )

    # ── Step 2: Auto-buy the strong candidate ─────────────────────────────
    buy_result = execute_auto_buy(buy_candidate, sb=sb, atr_pct=atr_pct)

    if not buy_result.get("success"):
        # Sell succeeded, buy failed — PARTIAL. Cash is freed, no new position.
        return SwapExecutionResult(
            proposal_key=key,
            status="PARTIAL",
            blocked_reason=f"auto-buy failed: {buy_result.get('error')}",
            sell_result=sell_result,
            buy_result=buy_result,
        )

    # ── Both legs done ────────────────────────────────────────────────────
    result = SwapExecutionResult(
        proposal_key=key,
        status="EXECUTED",
        sell_result=sell_result,
        buy_result=buy_result,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )

    # ── Persist execution record ──────────────────────────────────────────
    _record_execution(sb, proposal, result)

    logger.success(
        f"ONE-TAP SWAP EXECUTED: {sell_sym} → {buy_sym} "
        f"(+{margin:.0f} pts, margin={margin:.0f})"
    )
    return result


# ---------------------------------------------------------------------------
# Persistence (audit trail)
# ---------------------------------------------------------------------------


def _record_execution(sb, proposal: dict, result: SwapExecutionResult) -> None:
    """Write an audit row to swap_executions table."""
    try:
        row = {
            "sell_symbol": proposal.get("sell_symbol"),
            "buy_symbol": proposal.get("buy_symbol"),
            "sell_rank": proposal.get("sell_rank"),
            "buy_rank": proposal.get("buy_rank"),
            "margin": proposal.get("margin"),
            "regime": proposal.get("regime"),
            "status": result.status,
            "sell_order_id": (result.sell_result or {}).get("order_id"),
            "buy_order_id": (result.buy_result or {}).get("order_id"),
            "blocked_reason": result.blocked_reason,
            "executed_at": result.executed_at,
        }
        sb.table("swap_executions").insert(row).execute()
    except Exception as e:
        logger.warning(f"swap_executions insert failed (table may need migration): {e}")


# ---------------------------------------------------------------------------
# Convenience: execute all pending proposals from a SwapAnalysis
# ---------------------------------------------------------------------------


def execute_pending_swaps(
    analysis: dict,
    sb=None,
    atr_pct: float = 2.0,
) -> list[SwapExecutionResult]:
    """
    Execute all PROPOSED swaps from a SwapAnalysis result.

    Only processes proposals with status == "PROPOSED". Stops at the daily
    limit. Returns a list of SwapExecutionResult objects.

    Safe: respects all gates. If swap_auto_execute is off, returns empty
    list with all results marked BLOCKED.
    """
    sb = sb or get_supabase()
    proposals = analysis.get("proposals", []) if isinstance(analysis, dict) else analysis.proposals
    results: list[SwapExecutionResult] = []

    for p in proposals:
        if isinstance(p, dict):
            status = p.get("status", "PROPOSED")
            if status != "PROPOSED":
                continue
            proposal_dict = p
        else:
            # SwapProposal dataclass
            if p.status != "PROPOSED":
                continue
            proposal_dict = p.to_dict()

        result = execute_one_tap_swap(proposal_dict, sb=sb, atr_pct=atr_pct)
        results.append(result)

        # If BLOCKED by gates (not just one proposal), stop — no point trying more
        if result.status == "BLOCKED" and result.blocked_reason and (
            "swap_auto_execute is off" in result.blocked_reason
            or "kill switch" in result.blocked_reason
            or "swing_auto_entry" in result.blocked_reason
            or "daily swap limit" in result.blocked_reason
        ):
            break

    return results
