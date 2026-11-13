"""
TradeOS v7 — Portfolio Re-optimization Analysis
================================================

Batch analysis: given all open positions and all ranked candidates,
produce an optimized portfolio plan. Output is a REBALANCING PLAN —
which positions to keep, which to replace, which new ones to add.

Safe by design: reads only. Writes nothing. Places no orders.

The plan tells you what the optimal book would look like under the
current regime's position cap. You decide whether to act on it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from config import cfg_int, cfg_float


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RebalanceAction:
    """One action in the rebalancing plan."""

    symbol: str
    action: str             # KEEP | SELL | BUY | HOLD_AND_WATCH
    reason: str
    rank: float | None = None
    regime: str = "NEUTRAL"


@dataclass
class PortfolioPlan:
    """Complete re-optimization plan for one cycle."""

    regime: str
    max_positions: int
    current_positions: int
    actions: list[RebalanceAction] = field(default_factory=list)
    cash_freed_by_sells: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "max_positions": self.max_positions,
            "current_positions": self.current_positions,
            "actions": [a.__dict__ for a in self.actions],
            "cash_freed_by_sells": round(self.cash_freed_by_sells, 2),
            "note": self.note,
        }

    def sells(self) -> list[RebalanceAction]:
        return [a for a in self.actions if a.action == "SELL"]

    def buys(self) -> list[RebalanceAction]:
        return [a for a in self.actions if a.action == "BUY"]

    def keeps(self) -> list[RebalanceAction]:
        return [a for a in self.actions if a.action == "KEEP"]


# ---------------------------------------------------------------------------
# Helpers (shared parsing with swap_recommendations)
# ---------------------------------------------------------------------------


def _position_rank(position: dict) -> float | None:
    raw = position.get("entry_rationale") or ""
    if not raw:
        return None
    try:
        segment = raw.split("rank", 1)[1]
        number = segment.split("—")[0].strip()
        return float(number)
    except (IndexError, ValueError, AttributeError):
        return None


def _candidate_rank(candidate: dict) -> float | None:
    for key in ("composite_rank", "rank", "total_rank", "ai_rank"):
        v = candidate.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    tier = candidate.get("ai_tier")
    if tier is not None:
        try:
            return float(tier)
        except (TypeError, ValueError):
            pass
    return None


def _position_value(position: dict) -> float:
    val = position.get("invested_value")
    if val:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    qty = position.get("current_qty") or position.get("actual_qty") or 0
    px = position.get("current_price") or position.get("entry_price") or 0
    try:
        return float(qty) * float(px)
    except (TypeError, ValueError):
        return 0.0


def _is_active(position: dict) -> bool:
    return (position.get("status") or "ACTIVE").upper() == "ACTIVE"


# ---------------------------------------------------------------------------
# Core: reoptimize
# ---------------------------------------------------------------------------


def reoptimize_portfolio(
    open_positions: list[dict],
    candidates: list[dict],
    *,
    regime: str = "NEUTRAL",
    max_positions: int | None = None,
    margin: float | None = None,
    capital: float | None = None,
) -> PortfolioPlan:
    """
    Produce a portfolio re-optimization plan.

    Logic:
      1. Rank every active position (weakest first).
      2. Rank every candidate (strongest first).
      3. Keep positions up to the regime cap, starting with the strongest.
      4. Sell positions beyond the cap OR positions that a candidate beats
         by `margin` points (weakest first).
      5. Buy the strongest remaining candidates to fill freed slots.

    Parameters
    ----------
    open_positions : list[dict]
        Currently held positions.
    candidates : list[dict]
        Ranked buy candidates.
    regime : str
        Current market regime.
    max_positions : int | None
        Regime position cap. Defaults to config `max_positions_<regime>`
        or `max_positions_neutral` fallback (7).
    margin : float | None
        Minimum improvement to justify replacing a position.
        Defaults to config `swing_replace_margin` (20).
    capital : float | None
        Total capital — used for cash-freed estimates (informational only).

    Returns
    -------
    PortfolioPlan with `.actions` (KEEP / SELL / BUY).
    """
    if max_positions is None:
        key = f"max_positions_{regime.replace(' ', '_').lower()}"
        max_positions = cfg_int(key, cfg_int("max_positions_neutral", 7))
    if margin is None:
        margin = cfg_float("swing_replace_margin", 20.0)

    active = [p for p in open_positions if _is_active(p)]
    current_count = len(active)

    # Rank positions and candidates
    ranked_positions: list[tuple[dict, float]] = []
    for p in active:
        r = _position_rank(p)
        if r is not None:
            ranked_positions.append((p, r))
    ranked_positions.sort(key=lambda x: x[1], reverse=True)  # strongest first

    ranked_candidates: list[tuple[dict, float]] = []
    for c in candidates:
        r = _candidate_rank(c)
        if r is not None:
            ranked_candidates.append((c, r))
    ranked_candidates.sort(key=lambda x: x[1], reverse=True)  # strongest first

    held_symbols = {str(p.get("symbol", "")).upper() for p in active}
    candidate_symbols = {str(c.get("symbol", "")).upper() for c in candidates}

    actions: list[RebalanceAction] = []
    cash_freed = 0.0

    # ── Step 1: Keep the strongest positions up to the cap ──
    keep_count = min(max_positions, len(ranked_positions))
    kept_symbols: set[str] = set()
    for i, (pos, rank) in enumerate(ranked_positions):
        sym = str(pos.get("symbol", "")).upper()
        if i < keep_count:
            actions.append(RebalanceAction(
                symbol=str(pos.get("symbol")),
                action="KEEP",
                reason=(f"rank {rank:.0f} — within {regime} cap of {max_positions}"),
                rank=rank,
                regime=regime,
            ))
            kept_symbols.add(sym)
        else:
            # Beyond cap — candidate for selling
            actions.append(RebalanceAction(
                symbol=str(pos.get("symbol")),
                action="SELL",
                reason=(f"rank {rank:.0f} — beyond {regime} cap of {max_positions}; "
                        f"slot needed for stronger candidate"),
                rank=rank,
                regime=regime,
            ))
            cash_freed += _position_value(pos)

    # ── Step 2: Replace weak kept positions with better candidates ──
    # Among kept positions, find ones that a candidate beats by `margin`.
    # Sell the weakest kept position, buy the best available candidate.
    candidate_idx = 0
    for pos, pos_rank in ranked_positions:
        sym = str(pos.get("symbol", "")).upper()
        if sym not in kept_symbols:
            continue  # already marked for sell (beyond cap)

        # Find the best candidate not yet used and not already held
        while candidate_idx < len(ranked_candidates):
            cand, cand_rank = ranked_candidates[candidate_idx]
            cand_sym = str(cand.get("symbol", "")).upper()
            candidate_idx += 1
            if cand_sym in held_symbols or cand_sym in {a.symbol.upper() for a in actions if a.action == "BUY"}:
                continue
            break
        else:
            break  # no more candidates

        improvement = cand_rank - pos_rank
        if improvement >= margin:
            cand_sym = str(ranked_candidates[candidate_idx - 1][0].get("symbol"))
            # Convert the KEEP action to SELL + add a BUY
            for a in actions:
                if a.symbol.upper() == sym and a.action == "KEEP":
                    a.action = "SELL"
                    a.reason = (
                        f"rank {pos_rank:.0f} — replaced by {cand_sym} "
                        f"(rank {cand_rank:.0f}, {improvement:.0f} pts better)"
                    )
                    break
            actions.append(RebalanceAction(
                symbol=cand_sym,
                action="BUY",
                reason=(f"rank {cand_rank:.0f} — replaces {sym} "
                        f"(rank {pos_rank:.0f}, {improvement:.0f} pts better)"),
                rank=cand_rank,
                regime=regime,
            ))
            cash_freed += _position_value(pos)
            kept_symbols.discard(sym)

    # ── Step 3: Fill any remaining slots with strongest candidates ──
    current_keeps = sum(1 for a in actions if a.action == "KEEP")
    current_buys = sum(1 for a in actions if a.action == "BUY")
    slots_used = current_keeps + current_buys
    slots_remaining = max_positions - slots_used

    buy_idx = 0
    while slots_remaining > 0 and buy_idx < len(ranked_candidates):
        cand, cand_rank = ranked_candidates[buy_idx]
        buy_idx += 1
        cand_sym = str(cand.get("symbol", "")).upper()
        if cand_sym in held_symbols or cand_sym in {a.symbol.upper() for a in actions if a.action == "BUY"}:
            continue
        actions.append(RebalanceAction(
            symbol=str(cand.get("symbol")),
            action="BUY",
            reason=(f"rank {cand_rank:.0f} — new position, {slots_remaining} "
                    f"slot(s) remain under {regime} cap"),
            rank=cand_rank,
            regime=regime,
        ))
        slots_remaining -= 1

    # ── Step 4: Hold-and-watch for positions we can't rank ──
    for pos in active:
        sym = str(pos.get("symbol", "")).upper()
        if sym not in {a.symbol.upper() for a in actions}:
            actions.append(RebalanceAction(
                symbol=str(pos.get("symbol")),
                action="HOLD_AND_WATCH",
                reason="no parsable rank — retained pending manual review",
                regime=regime,
            ))

    # Sort actions: SELL first (free cash), then BUY, then KEEP, then HOLD_AND_WATCH
    order = {"SELL": 0, "BUY": 1, "KEEP": 2, "HOLD_AND_WATCH": 3}
    actions.sort(key=lambda a: order.get(a.action, 99))

    return PortfolioPlan(
        regime=regime,
        max_positions=max_positions,
        current_positions=current_count,
        actions=actions,
        cash_freed_by_sells=cash_freed,
        note=(
            f"{regime}: {current_count} positions, cap {max_positions}. "
            f"{len([a for a in actions if a.action == 'SELL'])} to sell, "
            f"{len([a for a in actions if a.action == 'BUY'])} to buy."
        ),
    )


# ---------------------------------------------------------------------------
# Convenience: quick "what should I sell" list
# ---------------------------------------------------------------------------


def positions_to_sell(
    open_positions: list[dict],
    candidates: list[dict],
    *,
    regime: str = "NEUTRAL",
    max_positions: int | None = None,
    margin: float | None = None,
) -> list[dict]:
    """Return positions flagged for SELL in the reoptimization plan."""
    plan = reoptimize_portfolio(
        open_positions, candidates, regime=regime,
        max_positions=max_positions, margin=margin,
    )
    sell_symbols = {a.symbol.upper() for a in plan.sells()}
    return [p for p in open_positions
            if str(p.get("symbol", "")).upper() in sell_symbols]
