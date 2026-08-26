"""
TradeOS v6 — apply an APPROVED SWING_ENGINE_LIFECYCLE proposal.

Phase 5 of the swing framework evolution blueprint, 26-Aug-2026. Not new
automation over review_swing_engine_lifecycle() (tools/weekly_review.py) —
that function is untouched, still runs Sundays, still only proposes to
brain_proposals. The one real gap this closes: nothing turned an APPROVED
verdict into an actual strategy_config.lifecycle change.

WHY THIS IS A SEPARATE TOOL, NOT A BRANCH INSIDE apply_proposal()
------------------------------------------------------------------
swing.brain.backtester_and_change_manager.apply_proposal() is the generic
dispatcher approve_proposal() calls automatically the moment a human marks
a proposal APPROVED. Found while building this: SWING_ENGINE_LIFECYCLE was
in NEITHER its AUTO_APPLICABLE nor REVIEW_ONLY sets — an unlisted type
falls through to that function's generic system_config upsert, which would
have written a strategy name ("RVS", "TPO", ...) into system_config as a
bogus config key the moment the operator approved the first one. Fixed at
the root (SWING_ENGINE_LIFECYCLE added to REVIEW_ONLY, same file), which
makes approve_proposal() correctly ACKNOWLEDGE the proposal and do nothing
further — matching this project's own "propose, never auto-apply" rule
for a strategy_config write specifically, which is a bigger decision than
a tunable config value and deserves its own explicit, separate step rather
than piggy-backing on the generic approval click.

USAGE
-----
    python -m tools.apply_swing_lifecycle           # apply every APPROVED row
    python -m tools.apply_swing_lifecycle --dry-run  # show what would change

Never automatic — run by hand, after reviewing what Sunday's job proposed
and marking the ones you agree with APPROVED via the normal
approve_proposal() flow.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config import get_supabase

VALID_LIFECYCLES = {"ACTIVE", "SHADOW", "RETIRED"}
# review_swing_engine_lifecycle() writes verdicts as PROMOTE/SHADOW/RETIRE/
# keep — PROMOTE and RETIRE are ACTIONS, not the lifecycle STATE
# strategy_config.lifecycle actually stores. This is the one translation
# step between "what was approved" and "what gets written".
VERDICT_TO_LIFECYCLE = {"PROMOTE": "ACTIVE", "SHADOW": "SHADOW", "RETIRE": "RETIRED"}


def apply_approved(dry_run: bool = False) -> list[dict]:
    """
    Applies every APPROVED SWING_ENGINE_LIFECYCLE proposal. Returns what
    was (or, in dry-run, would be) applied. Only ever touches APPROVED
    rows — PENDING is not consent, and re-running this after a row is
    already APPLIED is a no-op for that row (excluded from the query).
    """
    sb = get_supabase()
    rows = (sb.table("brain_proposals").select("*")
              .eq("proposal_type", "SWING_ENGINE_LIFECYCLE")
              .eq("status", "APPROVED").execute().data or [])
    if not rows:
        logger.info("  no APPROVED SWING_ENGINE_LIFECYCLE proposals to apply")
        return []

    applied = []
    for p in rows:
        strat = p.get("target_key")
        verdict = (p.get("proposed_value") or "").upper()
        lifecycle = VERDICT_TO_LIFECYCLE.get(verdict)
        if not strat or lifecycle not in VALID_LIFECYCLES:
            logger.error(f"  proposal {p.get('id')}: cannot apply — target_key="
                        f"{strat!r} proposed_value={p.get('proposed_value')!r} does "
                        f"not map to a valid lifecycle. Left APPROVED; fix or "
                        f"reject by hand.")
            continue

        cur = (sb.table("strategy_config").select("lifecycle")
                 .eq("strategy", strat).execute().data or [])
        if not cur:
            logger.error(f"  proposal {p.get('id')}: no strategy_config row for "
                        f"strategy={strat!r} — cannot apply. Left APPROVED.")
            continue
        old_lifecycle = cur[0].get("lifecycle")

        logger.info(f"  {strat}: {old_lifecycle} -> {lifecycle}"
                   + (" (dry run)" if dry_run else ""))
        if dry_run:
            applied.append({"strategy": strat, "from": old_lifecycle, "to": lifecycle,
                            "proposal_id": p.get("id")})
            continue

        sb.table("strategy_config").update({"lifecycle": lifecycle}).eq("strategy", strat).execute()
        sb.table("brain_proposals").update({
            "status": "APPLIED",
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "rollback_value": old_lifecycle,
        }).eq("id", p["id"]).execute()
        applied.append({"strategy": strat, "from": old_lifecycle, "to": lifecycle,
                        "proposal_id": p.get("id")})

    return applied


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    logger.info("=" * 60)
    logger.info(f"Apply swing engine lifecycle{' [DRY RUN]' if dry_run else ''}")
    logger.info("=" * 60)
    applied = apply_approved(dry_run=dry_run)
    logger.info(f"  {len(applied)} propos{'al' if len(applied)==1 else 'als'} "
               f"{'would be' if dry_run else ''} applied")


if __name__ == "__main__":
    main()
