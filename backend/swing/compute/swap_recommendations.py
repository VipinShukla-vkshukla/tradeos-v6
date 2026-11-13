"""
TradeOS v7 — Swap Recommendation Engine
========================================

Detects weak open positions and better candidates to replace them.
Generates swap PROPOSALS only — never executes orders.

A swap proposal says: "sell [weak symbol], buy [new symbol] because
[new candidate] ranks [X] points better than [weak symbol] by [margin]."

Safe by design: no order placement, no auto-execution, no live-money
side effects. Execution stays in your hands via the existing
Telegram → execution_engine.place_order() / place_exit_order() flow.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from config import cfg_float


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SwapProposal:
    """One proposed swap: sell weak, buy strong."""

    sell_symbol: str          # position to close
    buy_symbol: str           # candidate to open
    sell_rank: float          # rank of the position being sold
    buy_rank: float           # rank of the candidate replacing it
    margin: float             # buy_rank - sell_rank (points better)
    reason: str               # human-readable rationale
    regime: str               # regime at time of proposal
    status: str = "PROPOSED"  # PROPOSED | APPROVED | REJECTED | EXECUTED
    approved_by: str | None = None
    approved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sell_symbol": self.sell_symbol,
            "buy_symbol": self.buy_symbol,
            "sell_rank": self.sell_rank,
            "buy_rank": self.buy_rank,
            "margin": self.margin,
            "reason": self.reason,
            "regime": self.regime,
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


@dataclass
class SwapAnalysis:
    """Full swap analysis result for one cycle."""

    proposals: list[SwapProposal] = field(default_factory=list)
    weakest_position: dict | None = None
    best_candidate: dict | None = None
    total_positions: int = 0
    regime: str = "NEUTRAL"
    margin_used: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _position_rank(position: dict) -> float | None:
    """
    Extract the screener rank from a held position's entry_rationale.

    Format seen in practice: "rank 79 — screener 59 · AI tier 2 · ..."
    Returns None if no rank can be parsed (position can't be compared).
    """
    raw = position.get("entry_rationale") or ""
    if not raw:
        return None
    try:
        # "rank 79 — ..." → take the number after "rank"
        segment = raw.split("rank", 1)[1]
        number = segment.split("—")[0].strip()
        return float(number)
    except (IndexError, ValueError, AttributeError):
        return None


def _candidate_rank(candidate: dict) -> float | None:
    """Extract a candidate's composite rank from AI/screener fields."""
    # Prefer explicit composite rank if present
    for key in ("composite_rank", "rank", "total_rank", "ai_rank"):
        v = candidate.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    # Fallback: ai_conviction or ai_tier as a rough rank proxy
    tier = candidate.get("ai_tier")
    if tier is not None:
        try:
            return float(tier)
        except (TypeError, ValueError):
            pass
    return None


def _is_active(position: dict) -> bool:
    return (position.get("status") or "ACTIVE").upper() == "ACTIVE"


# ---------------------------------------------------------------------------
# Core: generate swap proposals
# ---------------------------------------------------------------------------


def generate_swap_proposals(
    open_positions: list[dict],
    candidates: list[dict],
    *,
    regime: str = "NEUTRAL",
    margin: float | None = None,
    max_proposals: int = 5,
) -> SwapAnalysis:
    """
    Compare every active open position against every candidate and produce
    swap proposals where a candidate materially beats a held position.

    Parameters
    ----------
    open_positions : list[dict]
        Currently held positions (from open_positions table).
    candidates : list[dict]
        Ranked buy candidates (from the screener / alert contenders).
    regime : str
        Current market regime — stored on each proposal for audit.
    margin : float | None
        Minimum rank improvement required to propose a swap.
        Defaults to config `swing_replace_margin` (20 points).
    max_proposals : int
        Cap the number of proposals returned (noise control).

    Returns
    -------
    SwapAnalysis with `.proposals` (best first) and summary fields.

    Safe: reads only. Writes nothing. Places no orders.
    """
    if margin is None:
        margin = cfg_float("swing_replace_margin", 20.0)

    active_positions = [p for p in open_positions if _is_active(p)]
    total = len(active_positions)

    # Rank every position we can parse
    ranked_positions: list[tuple[dict, float]] = []
    for p in active_positions:
        r = _position_rank(p)
        if r is not None:
            ranked_positions.append((p, r))

    # Rank every candidate we can score
    ranked_candidates: list[tuple[dict, float]] = []
    for c in candidates:
        r = _candidate_rank(c)
        if r is not None:
            ranked_candidates.append((c, r))

    # Sort: weakest position first (lowest rank = weakest)
    ranked_positions.sort(key=lambda x: x[1])
    # Sort: strongest candidate first (highest rank = best)
    ranked_candidates.sort(key=lambda x: x[1], reverse=True)

    # Build a set of symbols already held so we don't propose swapping IN
    # something we already own (that would be a scale-add, not a swap).
    held_symbols = {str(p.get("symbol", "")).upper() for p in active_positions}

    proposals: list[SwapProposal] = []
    used_buyers: set[str] = set()   # don't propose the same candidate twice
    used_sellers: set[str] = set()  # don't propose selling the same position twice

    weakest_position = ranked_positions[0][0] if ranked_positions else None
    best_candidate = ranked_candidates[0][0] if ranked_candidates else None

    for pos, pos_rank in ranked_positions:
        pos_sym = str(pos.get("symbol", "")).upper()
        if pos_sym in used_sellers:
            continue

        for cand, cand_rank in ranked_candidates:
            cand_sym = str(cand.get("symbol", "")).upper()
            if cand_sym in used_buyers:
                continue
            if cand_sym == pos_sym:
                continue  # can't swap a position into itself
            if cand_sym in held_symbols:
                continue  # already held — not a swap candidate

            improvement = cand_rank - pos_rank
            if improvement >= margin:
                proposal = SwapProposal(
                    sell_symbol=str(pos.get("symbol")),
                    buy_symbol=str(cand.get("symbol")),
                    sell_rank=round(pos_rank, 1),
                    buy_rank=round(cand_rank, 1),
                    margin=round(improvement, 1),
                    reason=(
                        f"{cand_sym} ranks {cand_rank:.0f} vs your held "
                        f"{pos_sym} at {pos_rank:.0f} — {improvement:.0f} "
                        f"points better ({regime} regime)"
                    ),
                    regime=regime,
                )
                proposals.append(proposal)
                used_sellers.add(pos_sym)
                used_buyers.add(cand_sym)
                break  # move to next weakest position

        if len(proposals) >= max_proposals:
            break

    # Sort proposals by margin descending (best swap first)
    proposals.sort(key=lambda p: p.margin, reverse=True)

    return SwapAnalysis(
        proposals=proposals,
        weakest_position=weakest_position,
        best_candidate=best_candidate,
        total_positions=total,
        regime=regime,
        margin_used=margin,
    )


def find_positions_to_sell(
    open_positions: list[dict],
    candidates: list[dict],
    *,
    regime: str = "NEUTRAL",
    margin: float | None = None,
) -> list[dict]:
    """
    Return the subset of open positions that have a better candidate waiting.
    Useful for a "these positions are candidates for exit" list.
    """
    analysis = generate_swap_proposals(
        open_positions, candidates, regime=regime, margin=margin,
    )
    sell_symbols = {p.sell_symbol.upper() for p in analysis.proposals}
    return [p for p in open_positions if str(p.get("symbol", "")).upper() in sell_symbols]


# ---------------------------------------------------------------------------
# Approval tracking (in-memory; persistence is the caller's choice)
# ---------------------------------------------------------------------------


class SwapProposalStore:
    """
    In-memory store for swap proposals with approval status.

    Persistence (Supabase table) is intentionally left to the caller — this
    module only holds proposals in memory during a session. If you want
    proposals to survive restarts, serialize `.to_dict()` to a table or file.
    """

    def __init__(self) -> None:
        self._proposals: dict[str, SwapProposal] = {}

    def add(self, proposal: SwapProposal) -> None:
        key = f"{proposal.sell_symbol}→{proposal.buy_symbol}"
        self._proposals[key] = proposal

    def get(self, sell_symbol: str, buy_symbol: str) -> SwapProposal | None:
        return self._proposals.get(f"{sell_symbol}→{buy_symbol}")

    def approve(self, sell_symbol: str, buy_symbol: str, approved_by: str) -> bool:
        """Mark a proposal as approved. Does NOT place any order."""
        key = f"{sell_symbol}→{buy_symbol}"
        p = self._proposals.get(key)
        if not p or p.status != "PROPOSED":
            return False
        from datetime import datetime, timezone

        p.status = "APPROVED"
        p.approved_by = approved_by
        p.approved_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"SWAP APPROVED: sell {sell_symbol} → buy {buy_symbol} "
                    f"by {approved_by}")
        return True

    def reject(self, sell_symbol: str, buy_symbol: str) -> bool:
        key = f"{sell_symbol}→{buy_symbol}"
        p = self._proposals.get(key)
        if not p or p.status != "PROPOSED":
            return False
        p.status = "REJECTED"
        return True

    def all_proposals(self) -> list[SwapProposal]:
        return list(self._proposals.values())

    def pending(self) -> list[SwapProposal]:
        return [p for p in self._proposals.values() if p.status == "PROPOSED"]

    def approved(self) -> list[SwapProposal]:
        return [p for p in self._proposals.values() if p.status == "APPROVED"]
