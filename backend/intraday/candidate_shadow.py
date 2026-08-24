"""
Stage D6 — runs every operator-approved templated candidate against the
live book, in shadow. docs/TRADEOS_ROADMAP.md, Track D, Stage D6.

Same isolation guarantee migration 105 (Stage D3's event_core.py) gives
its own shadow table: reads `engine._contexts` — the SAME contexts the
trusted polling loop already builds every 300s, nothing refetched —
writes ONLY to `intraday_candidate_shadow` (migration 109), never
`intraday_setups`, never `execution.paper_broker`, never
`allocation.allocator`. A bug in `candidate_template.py`'s own detection
logic can pollute only its own shadow log.

RUNS ON THE SLOW TIMER, NOT TICK-TRIGGERED, DELIBERATELY. Unlike Stage
D3's event core, nothing here needs sub-15s reaction: a gap-based
condition is fixed at the open, and the other translated features
describe YESTERDAY and do not change intraday at all — nothing about
this stage's own signal is lost by evaluating it on run.py's existing
300s slow timer, alongside every other periodic refresh.

DEDUPED PER (trade_date, proposal, symbol) — NOT PER CALL. A candidate
whose condition stays true all session must not be re-logged every slow-
timer tick as though it were a fresh detection each time; that is the
"one setup counted eleven times" defect this project has paid for twice
already (RNG's n=11; this same branch's own same-day-calibration bug,
found and fixed earlier this session). One batched read of today's
already-logged (proposal, symbol) pairs per check(), not one query per
candidate per symbol.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_bool, today_ist

from intraday.candidate_template import TemplatedCandidate, evaluate, from_proposal


def load_active_candidates(sb) -> list[TemplatedCandidate]:
    """
    Every ENGINE_CANDIDATE proposal the operator has explicitly approved
    and that from_proposal() can actually build a candidate from. A row
    that cannot be templated is logged and skipped, never silently
    dropped.

    status='APPROVED', NOT A NEW LITERAL — swing/brain/backtester_and_
    change_manager.py::approve_proposal() already sets exactly this
    status for ENGINE_CANDIDATE rows (confirmed live: proposals #188/#190
    became GDB this way), and ENGINE_CANDIDATE's own membership in that
    module's REVIEW_ONLY set is what makes APPROVED safe for this type —
    apply_proposal() acknowledges and returns without writing system_
    config or strategy_config for anything in REVIEW_ONLY. Inventing a
    second, parallel "approved" status here would have fragmented one
    real human decision into two different fields nothing kept in sync,
    the exact near-homophone risk docs/TERMINOLOGY.md exists to prevent.
    """
    try:
        rows = (sb.table("brain_proposals")
                  .select("id,target_key,evidence,confidence")
                  .eq("proposal_type", "ENGINE_CANDIDATE")
                  .eq("status", "APPROVED")
                  .execute().data or [])
    except Exception as e:
        logger.debug(f"  candidate_shadow: could not load approved candidates — {e}")
        return []

    out = []
    for r in rows:
        cand, reason = from_proposal(r)
        if cand is None:
            logger.debug(f"  candidate_shadow: proposal #{r.get('id')} skipped — {reason}")
            continue
        out.append(cand)
    return out


def check(engine) -> int:
    """
    One shadow pass over every approved candidate x every context the
    trusted loop already built. Returns how many NEW shadow detections
    were logged this call.

    ADVISORY ONLY. A failure anywhere in here must never take the caller
    down — the trusted polling loop's own cycle is what actually protects
    positions and enters trades; this runs alongside it, never in place
    of it.
    """
    if not cfg_bool("intraday_candidate_shadow_enabled", False):
        return 0
    if engine is None or getattr(engine, "sb", None) is None:
        return 0

    candidates = load_active_candidates(engine.sb)
    if not candidates:
        return 0

    try:
        from intraday.session import session_state
        phase = session_state().phase
    except Exception as e:
        logger.debug(f"  candidate_shadow: session state unavailable — {e}")
        return 0

    trade_date = today_ist().isoformat()
    detected_at = datetime.now(IST)

    try:
        existing = (engine.sb.table("intraday_candidate_shadow")
                      .select("proposal_id,symbol")
                      .eq("trade_date", trade_date).execute().data or [])
        already = {(r["proposal_id"], r["symbol"]) for r in existing}
    except Exception as e:
        logger.debug(f"  candidate_shadow: dedup read failed — {e}")
        already = set()

    logged = 0
    for cand in candidates:
        for sym, ctx in (engine._contexts or {}).items():
            if (cand.proposal_id, sym) in already:
                continue
            try:
                setup = evaluate(cand, ctx, phase)
            except Exception as e:
                logger.debug(f"  candidate_shadow: evaluation failed for "
                            f"{sym}/{cand.name} — {e}")
                continue
            if setup is None:
                continue
            try:
                engine.sb.table("intraday_candidate_shadow").insert({
                    "trade_date":   trade_date,
                    "proposal_id":  cand.proposal_id,
                    "feature_name": cand.feature_name,
                    "symbol":       setup.symbol,
                    "direction":    setup.direction,
                    "entry":        setup.entry,
                    "stop":         setup.stop,
                    "target":       setup.target,
                    "confidence":   setup.confidence,
                    "rationale":    setup.rationale,
                    "detected_at":  detected_at.isoformat(),
                    # Same sanitization event_core.py's own shadow insert
                    # uses — a stray non-JSON-native type in meta (e.g. a
                    # numpy float from an indicator) must not fail the
                    # whole insert.
                    "meta":         json.loads(json.dumps(setup.meta or {}, default=str)),
                }).execute()
                already.add((cand.proposal_id, sym))
                logged += 1
            except Exception as e:
                logger.debug(f"  candidate_shadow: shadow log failed for "
                            f"{sym}/{cand.name} — {e}")

    return logged
