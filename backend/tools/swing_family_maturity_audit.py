"""
Which swing families' numbers can actually be trusted right now — per
family, not book-wide. Read-only, swing-only; touches no intraday table,
no intraday code, and writes nothing.

WHY PER-FAMILY, NOT THE EXISTING BOOK-WIDE CHECK
--------------------------------------------------
`allocation.scoring._swing_bias_warning()` already says the R prior rests
on a young, survivorship-in-time-biased sample: fast winners resolve first,
so a young dataset over-represents them. That check is book-wide — one
verdict for all of swing. Now that `allocation/swing_hold_days.py` gives
each family its OWN edge and its own hold-days estimate (07-Aug-2026), the
question has to be asked per family too: a family whose typical setup takes
15 sessions to resolve is exactly the one whose resolved sample is thinnest
and most biased toward the outliers that happened to resolve fast, even
while the BOOK-WIDE sample looks adequate.

WHAT THIS MEASURES
-------------------
Per family: how many signals have ever been entered, how many of those have
actually resolved (TARGET or STOP — the same "usable R" definition
_swing_bias_warning uses), the date span the resolved ones cover, and —
reusing Point 3's own function so the two never quietly disagree — the
per-family hold-days estimate now feeding the allocator, when that family
clears its floor.

A family with a low resolved-to-entered ratio is exactly the one whose
edge and hold-days figures should be treated with the least confidence,
regardless of what its raw n looks like in isolation.

Usage:
    python -m tools.swing_family_maturity_audit
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from config import get_supabase, fetch_all

PAGE = 1000


def audit(sb=None) -> None:
    from allocation.scoring import swing_family
    from allocation.swing_hold_days import expected_hold_days_by_family

    sb = sb or get_supabase()
    logger.info("=" * 78)
    logger.info("SWING FAMILY MATURITY AUDIT — per-family, not book-wide")
    logger.info("=" * 78)

    # SORTED PAGING, (symbol, date) — signal_output_daily has no `id` column,
    # so fetch_all's default order key raises 42703 on it.
    rows = fetch_all(lambda: sb.table("signal_output_daily")
                     .select("strategy,date,outcome_entered,outcome_category,"
                             "symbol")
                     .eq("outcome_entered", True),
                     page=PAGE, order_by="symbol,date")

    if not rows:
        logger.warning("  no entered signals found — nothing to measure")
        return

    by_family: dict[str, dict] = {}
    for r in rows:
        fam = swing_family(r.get("strategy"))
        d = by_family.setdefault(fam, {"entered": 0, "resolved": 0, "dates": []})
        d["entered"] += 1
        if r.get("outcome_category") in ("TARGET", "STOP"):
            d["resolved"] += 1
            if r.get("date"):
                d["dates"].append(str(r["date"]))

    hold_days = expected_hold_days_by_family(sb)

    logger.info(f"  {'family':<14}{'entered':>9}{'resolved':>10}{'resolved%':>11}"
                f"{'span':>24}{'hold_days':>12}")
    logger.info("  " + "-" * 76)
    for fam in sorted(by_family, key=lambda f: -by_family[f]["entered"]):
        d = by_family[fam]
        pct = d["resolved"] / d["entered"] * 100.0 if d["entered"] else 0.0
        span = f"{min(d['dates'])} to {max(d['dates'])}" if d["dates"] else "—"
        hd = hold_days.get(fam)
        hd_str = f"{hd[0]:.1f}d (n={hd[1]})" if hd else "book-pooled"
        flag = "  ⚠ thin/unresolved" if pct < 50 or d["resolved"] < 20 else ""
        logger.info(f"  {fam:<14}{d['entered']:>9}{d['resolved']:>10}{pct:>10.0f}%"
                    f"{span:>24}{hd_str:>12}{flag}")

    logger.info("")
    logger.info("  A family flagged above has the least trustworthy edge AND "
                "hold-days estimate right now — its resolved sample is either "
                "thin (<20) or dominated by whatever happened to resolve fast "
                "(<50% of entered signals have resolved at all). Treat its "
                "numbers as provisional regardless of how confident the raw "
                "n looks in isolation.")


def main() -> int:
    audit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
