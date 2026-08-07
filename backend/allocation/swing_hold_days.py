"""
Per-engine-family hold_days — SWING ONLY.

WHY THIS IS ITS OWN FILE, NOT A FUNCTION ADDED TO scoring.py
--------------------------------------------------------------
Operator's explicit instruction, 07-Aug-2026: this branch touches swing
only, and anything that could affect intraday's live behaviour must be
isolated into its own file rather than a conditional buried inside shared
code. `allocation/scoring.py` and `allocation/allocator.py` are genuinely
shared — both books' proposals flow through `Proposal`, `score()`,
`Allocator.select()` — and this codebase has already paid once for
duplicating decision logic (CLAUDE.md: "three divergent R:R models giving
three answers for one stock on one day"), so the fix is not a second copy
of `score()` or `expected_hold_days()`. It is: keep the shared functions
untouched, and put the SWING-specific computation somewhere intraday's
code never imports from at all. `allocator.py`'s only change is a few
lines that call into this module, gated to the SWING branch of a loop that
already exists — see that file's own comments at the call site.

WHY PER-FAMILY AT ALL
----------------------
`scoring.expected_hold_days(sb, "SWING")` returns ONE number, pooled across
the entire book, and every swing proposal divides its edge by that same
constant regardless of which engine produced it — a CTL continuation play
and an SBS setup, whose typical hold times are not necessarily the same
thing, are compared as if they were. This buckets by `scoring.swing_family
(strategy)` — the SAME mapping `swing_priors()` already uses to key the R
prior itself — so a family's edge and its own hold-days estimate are
measured on the same population, not a book-wide average standing in for
one.

NOT CALLED FOR INTRADAY, EVER
-------------------------------
Intraday resolves same-day by construction (flat by 15:15 IST) — there is
no per-engine hold-time question to ask there. `Allocator.select()` never
calls this module for the INTRADAY branch of its loop.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg_int

PAGE = 1000


def expected_hold_days_by_family(sb) -> dict[str, tuple[float, int]]:
    """
    {family: (mean_hold_days, n)} from SWING's own closed_positions.

    Same query shape as scoring.expected_hold_days(), scoped to SWING and
    bucketed by family instead of pooled. A family below `hold_days_min_
    sample` (default 5 — a mean needs far less data to stabilise than the
    full R distribution Prior holds to a 30-sample floor, so a smaller,
    separately-justified floor here is deliberate, not a shortcut) is
    excluded from the returned dict entirely — the caller's job
    (Allocator.select()) is to fall back to the book-pooled figure when a
    family isn't here, exactly as if this function had never been called,
    not to trust a mean of two closes.
    """
    from allocation.scoring import swing_family
    floor = cfg_int("hold_days_min_sample", 5)

    rows, off = [], 0
    while True:
        page = (sb.table("closed_positions")
                  .select("strategy,hold_days")
                  .eq("framework", "SWING")
                  .not_.is_("hold_days", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    by_family: dict[str, list[float]] = {}
    for r in rows:
        d = r.get("hold_days")
        if d is None:
            continue
        try:
            days = max(float(d), 0.5)   # same-day close = half a day, matching expected_hold_days()
        except (TypeError, ValueError):
            continue
        fam = swing_family(r.get("strategy"))
        by_family.setdefault(fam, []).append(days)

    return {fam: (statistics.fmean(v), len(v)) for fam, v in by_family.items()
            if len(v) >= floor}
