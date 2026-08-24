"""
Stage D6 — explicit, one-proposal-at-a-time approval for an
ENGINE_CANDIDATE row. docs/TRADEOS_ROADMAP.md, Track D, Stage D6.

    python -m tools.approve_candidate --id 123           # approve
    python -m tools.approve_candidate --id 123 --dry      # check only

REUSES THE EXISTING APPROVAL MECHANISM — DOES NOT INVENT A NEW ONE
----------------------------------------------------------------------
`status=APPROVED` is already the real, precedented human-approval status
for this exact proposal type: swing/brain/backtester_and_change_manager.py
::approve_proposal() sets it, and ENGINE_CANDIDATE's own membership in
that module's REVIEW_ONLY set is what makes it SAFE — the immediate
apply_proposal() call it makes acknowledges and returns for any
REVIEW_ONLY type, never reaching the system_config/strategy_config write
path. Verified live: proposals #188/#190 (brain_proposals) became GDB
this exact way. A first version of this tool set a brand-new status
(`SHADOW_APPROVED`) before this precedent was found, which would have
fragmented one human decision into two different fields nothing kept in
sync — corrected before it ever shipped.

This tool adds exactly one thing approve_proposal() does not have on its
own: it refuses to approve a row candidate_template.py cannot actually
use, so "approved" and "will produce shadow activity" never diverge
silently.

Sets status=APPROVED via the existing function — read by intraday/
candidate_shadow.py::load_active_candidates(). Approving a candidate only
makes it ELIGIBLE for shadow detection; nothing runs unless
intraday_candidate_shadow_enabled is also armed (ships false, a separate
switch, same "capture vs act" split every Track D stage this session has
used).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase

from intraday.candidate_template import from_proposal


def decide(row: dict) -> tuple[bool, str]:
    """
    Pure: should THIS row be approved, and why (or why not)? No I/O — a
    caller decides what to do with the answer. Separated from main() so
    the decision logic is testable offline the same way every evaluate()
    in this codebase is, rather than only exercisable through a live DB
    call.
    """
    proposal_id = row.get("id")
    if row.get("proposal_type") != "ENGINE_CANDIDATE":
        return False, (f"proposal #{proposal_id} is "
                       f"{row.get('proposal_type')!r}, not ENGINE_CANDIDATE "
                       f"— refusing")
    if row.get("status") != "PENDING":
        return False, (f"proposal #{proposal_id} is already "
                       f"{row.get('status')!r}, not PENDING — refusing to "
                       f"re-approve")

    # Confirm this row is actually something candidate_template.py can use
    # BEFORE approving it — approving a row that will produce zero shadow
    # activity is a real, silent failure mode ("I could not compute this
    # is a required finding", not a thing to discover a week later from an
    # empty shadow log).
    cand, reason = from_proposal(row)
    if cand is None:
        return False, (f"proposal #{proposal_id} "
                       f"({row.get('target_key')}): {reason} — approving "
                       f"anyway will produce NO shadow activity, refusing")

    return True, (f"proposal #{proposal_id}: {row.get('target_key')} — "
                  f"feature={cand.feature_name}, "
                  f"avg_move_pct={cand.avg_move_pct:.2f}, "
                  f"lift={cand.lift:.1f}x, n_miss={cand.n_miss}, "
                  f"confidence={cand.confidence:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", type=int, required=True, help="brain_proposals.id")
    ap.add_argument("--dry", action="store_true",
                    help="check only, write nothing")
    args = ap.parse_args()

    sb = get_supabase()
    rows = (sb.table("brain_proposals")
              .select("id,proposal_type,target_key,status,evidence,confidence")
              .eq("id", args.id).execute().data or [])
    if not rows:
        logger.error(f"  no brain_proposals row with id={args.id}")
        return

    ok, detail = decide(rows[0])
    (logger.info if ok else logger.warning)(f"  {detail}")
    if not ok:
        return

    if args.dry:
        logger.info(f"  [dry] would approve proposal #{args.id} "
                    f"(status=APPROVED, via approve_proposal())")
        return

    # REUSED, NOT REIMPLEMENTED — same function swing/brain's own review
    # flow already calls for a human-approved ENGINE_CANDIDATE.
    from swing.brain.backtester_and_change_manager import approve_proposal
    ok = approve_proposal(args.id, reviewer="operator")
    if ok:
        logger.success(f"  proposal #{args.id} approved (CAND{args.id}) — "
                       f"armed only once intraday_candidate_shadow_enabled "
                       f"is also true")
    else:
        logger.error(f"  approve_proposal() returned False for #{args.id} "
                     f"— check its own log output above")


if __name__ == "__main__":
    main()
