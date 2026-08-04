"""
Put Phase 4 back the way it was, without a deploy.

    python -m tools.rollback                  what is on, and what it was before
    python -m tools.rollback --status         the same, explicitly
    python -m tools.rollback --allocator-off  stand the allocator down only
    python -m tools.rollback --all-off        every Phase 4 switch to pre-Phase-4

WHY A TOOL AND NOT A GIT REVERT
-------------------------------
There are three rollback layers and only the first one is fast: configuration,
then the kill switch, then reverting the merge and redeploying. The one that
matters is configuration, because it works identically before or after a merge
and it works at 10:47 on a Tuesday from a phone. Reverting code is what you do
afterwards, calmly. A rollback you have to think about is one whose shape you
discover during the incident.

WHERE THE "BEFORE" VALUE COMES FROM
-----------------------------------
Not from a list in this file. system_config already carries a default_value
column per row, so each key records its own pre-Phase-4 value next to itself
and there is nothing here to drift out of step with the migration that created
it. This file holds only the *membership* question — which keys Phase 4 owns —
because that is the part a human needs to audit, and it is checked in both
directions: a registered key missing from the database is reported, and a
Phase-4-shaped key in the database that nobody registered is reported louder.
That second one is the failure that would make --all-off quietly incomplete.

THIS TOOL WRITES TO system_config AND NOTHING ELSE.
It never places an order, never touches a position, never alters a schema.
Standing the allocator down cannot itself move money.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


# ── What Phase 4 owns ───────────────────────────────────────────────────────
#
# (key, stage, what turning it off gives back). Each stage appends its own
# switches here as it lands. A switch that is not in this list is not restored
# by --all-off, which is why the drift check below exists.
SWITCHES: list[tuple[str, int, str]] = [
    ("alloc_live_swing", 10,
     "the swing book stops asking the allocator which plan gets the entry and "
     "returns to the greedy path — the ranked-first plan wins, as before"),
    ("alloc_live_intraday", 10,
     "the intraday book returns to taking the highest-confidence setup that "
     "clears its gates, with no opportunity-cost comparison"),
    ("alloc_shadow_enabled", 10,
     "the allocator stops recording verdicts entirely. Note this destroys the "
     "evidence the promotion gate is denominated in, so it is the LAST thing "
     "to turn off, not the first"),
    ("storage_rolloff_enabled", 1,
     "the evening pipeline stops calling archive_stock_data(); the function "
     "goes back to existing and never running, as it did from Feb to Aug 2026"),
]

# Keys shaped like Phase 4 work. A BOOL key matching one of these that is NOT
# registered above is drift — a switch --all-off would silently walk past.
#
# The bool test is what makes the drift check usable rather than noisy. It first
# flagged storage_rolloff_keep_days, which is a retention window and not a
# switch: restoring it would mean "roll off a different amount of history",
# which is not a rollback of anything. --all-off restores switches; tuning
# values are left where the operator put them.
PHASE4_PREFIXES = ("allocator_", "storage_rolloff", "hurdle_", "proposal_")

ALLOCATOR_STAGE = 10


def _registry(allocator_only: bool = False) -> list[tuple[str, int, str]]:
    if allocator_only:
        return [s for s in SWITCHES if s[1] == ALLOCATOR_STAGE or s[0].startswith("allocator_")]
    return SWITCHES


def daemon_honours_config() -> tuple[bool, str]:
    """
    Does a change made here actually reach a daemon that is already running?

    THE ANSWER WAS NO UNTIL 04-Aug-2026, AND NOTHING SAID SO.

    config.get_system_config() caches on first read. Nothing in the daemon
    refreshed it, so a process that booted at 09:00 answered every cfg_*() call
    for the rest of the session from the 09:00 snapshot — including the kill
    switch it re-checks every second. Setting a switch mid-session stored it,
    displayed it as set, and changed nothing.

    That makes this tool's entire promise conditional on one line in the
    daemon's slow timer. So it is asserted rather than assumed, the same way
    check_exit_actions asserts its three lists still agree: by reading the
    source, so it fails the moment someone removes the refresh, not the next
    time somebody needs the switch to work.
    """
    src = (Path(__file__).resolve().parent.parent / "intraday/run.py").read_text(encoding="utf-8")
    if not re.search(r"get_system_config\(\s*refresh\s*=\s*True\s*\)", src):
        return False, ("intraday/run.py never refreshes system_config — every switch "
                       "below is invisible to a daemon that is already running, and "
                       "so is the kill switch. This tool cannot roll anything back "
                       "until that refresh is restored on the slow timer.")
    return True, "daemon re-reads system_config on its 300 s timer (worst-case lag 5 min)"


def _rows(sb, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    got = (sb.table("system_config")
             .select("key,value,default_value,description")
             .in_("key", keys).execute().data) or []
    return {r["key"]: r for r in got}


def status(sb) -> int:
    ok, why = daemon_honours_config()
    logger.info("═" * 72)
    logger.info("Phase 4 switches")
    logger.info("═" * 72)
    (logger.success if ok else logger.error)(f"  {'✓' if ok else '✗'}  {why}")
    logger.info("")

    reg  = _registry()
    rows = _rows(sb, [k for k, _, _ in reg])

    if not reg:
        logger.info("  no Phase 4 switches exist yet")
    for key, stage, gives_back in reg:
        r = rows.get(key)
        if r is None:
            logger.warning(f"  ?  {key:<32} not in system_config "
                           f"(migration for stage {stage} not applied)")
            continue
        cur, was = str(r.get("value")), str(r.get("default_value"))
        if cur == was:
            logger.info(f"  ·  {key:<32} {cur:<8} (already at pre-Phase-4 value)")
        else:
            logger.info(f"  ●  {key:<32} {cur:<8} → --all-off restores {was}")
            logger.info(f"       gives back: {gives_back}")

    # Drift: a Phase-4-shaped switch nobody registered. --all-off would skip it.
    known = {k for k, _, _ in reg}
    live  = (sb.table("system_config").select("key,value_type").execute().data) or []
    stray = sorted(r["key"] for r in live
                   if r["key"].startswith(PHASE4_PREFIXES)
                   and (r.get("value_type") or "").lower() in ("bool", "boolean")
                   and r["key"] not in known)
    if stray:
        logger.error("")
        logger.error(f"  ✗  {len(stray)} Phase-4 switch(es) not registered in this tool: "
                     f"{', '.join(stray)}")
        logger.error("     --all-off would walk past them. Add them to SWITCHES.")
        return 1
    return 0 if ok else 1


def turn_off(sb, allocator_only: bool) -> int:
    ok, why = daemon_honours_config()
    if not ok:
        logger.error(f"REFUSING: {why}")
        return 1

    reg  = _registry(allocator_only)
    rows = _rows(sb, [k for k, _, _ in reg])
    if not reg:
        logger.info("nothing registered to turn off"
                    + (" — the allocator does not exist yet (stage 10)" if allocator_only else ""))
        return 0

    changed, missing = [], []
    for key, stage, gives_back in reg:
        r = rows.get(key)
        if r is None:
            missing.append(key)
            continue
        cur, was = str(r.get("value")), str(r.get("default_value"))
        if was in ("None", ""):
            logger.error(f"  ✗  {key} has no default_value recorded — refusing to guess "
                         f"what it was before Phase 4")
            return 1
        if cur == was:
            continue
        sb.table("system_config").update({"value": was}).eq("key", key).execute()
        changed.append((key, cur, was, gives_back))

    logger.info("═" * 72)
    logger.info("allocator stood down" if allocator_only else "Phase 4 switches restored")
    logger.info("═" * 72)
    if not changed:
        logger.info("  nothing to change — every switch was already at its pre-Phase-4 value")
    for key, cur, was, gives_back in changed:
        logger.success(f"  ✓  {key}: {cur} → {was}")
        logger.info(f"       {gives_back}")
    for key in missing:
        logger.warning(f"  ?  {key} not in system_config — its migration has not been applied")

    if changed:
        logger.info("")
        logger.info("  A running daemon picks this up within 300 s. It does NOT close "
                    "open positions — broker-side stops still protect them.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Roll Phase 4 back through configuration alone")
    g  = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="what is on and what it was before")
    g.add_argument("--allocator-off", action="store_true", help="stand the allocator down only")
    g.add_argument("--all-off", action="store_true",
                   help="every Phase 4 switch back to its pre-Phase-4 value")
    a = ap.parse_args(argv)

    from config import get_supabase
    sb = get_supabase()

    if a.all_off:
        return turn_off(sb, allocator_only=False)
    if a.allocator_off:
        return turn_off(sb, allocator_only=True)
    return status(sb)          # default, because the safe action is to look first


if __name__ == "__main__":
    sys.exit(main())
