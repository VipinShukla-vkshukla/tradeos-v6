"""
The min_score_to_show floor in generate_signals, as pure functions.

Two defects lived in one `if score < min_score: continue`:

1. It ran BEFORE a held name was classified. A held position is classified on
   holding_score / lifecycle / st_cushion_pct, never on final_score, so the
   floor removed the weakest holdings first - exactly the ones whose EXIT or
   REDUCE alert the operator needs. Held names now bypass it.

2. When it emptied the whole shortlist (10-Aug..05-Oct-2026: max final_score
   49.0, 41.8, 46.6 against a floor of 50) nothing said so. The step logged
   "No signals to write", reported OK, and the dashboard and audit both read
   the frozen signal_log date as a stale pipeline and said to re-run it,
   which reproduces the same empty result. Callers use `floor_state` to name
   the real reason.

No database, no config reads: the caller passes the numbers in.
"""
from __future__ import annotations


def passes_score_floor(score: float, floor: float, held: bool) -> bool:
    """A held name is never dropped by the show-floor; everything else needs score >= floor."""
    return held or score >= floor


def floor_state(scores: list[float], floor: float) -> dict:
    """
    Summarise a shortlist against the floor.

    all_below is True only for a NON-EMPTY shortlist with no row at or above the
    floor. An empty shortlist is a different fault (nothing screened) and must
    not be explained away as "below the floor".
    """
    total = len(scores)
    top = max(scores) if scores else None
    return {
        "total":     total,
        "below":     sum(1 for s in scores if s < floor),
        "max_score": top,
        "floor":     floor,
        "all_below": total > 0 and top < floor,
    }


def describe_all_below(state: dict) -> str:
    return (
        f"all {state['total']} shortlist rows are below min_score_to_show "
        f"(max {state['max_score']:.1f} < {state['floor']:g}) - "
        f"nothing cleared the floor. Re-running the pipeline will not change this."
    )
