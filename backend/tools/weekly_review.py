"""
The weekly learning pass — measure, then PROPOSE. Never apply.

    python -m tools.weekly_review          write proposals
    python -m tools.weekly_review --show   read the open ones

WHY WEEKLY AND NOT DAILY
------------------------
A day produces 20-40 intraday detections and at most two swing closes. Re-tuning
on that is chasing last Tuesday: the sample is too small to separate an edge from
a run of luck, and a system that adjusts every night converges on noise while
appearing to learn. A week gives roughly 150 detections and enough swing
outcomes to move a threshold with a straight face.

WHY IT PROPOSES RATHER THAN APPLIES
-----------------------------------
Auto-applied changes make a framework nobody can reason about. Six weeks of
nightly self-tuning and the answer to "why is the stop 0.42%" is "the machine
decided", which is the point at which you can no longer tell a working system
from a broken one. Proposals keep the loop closed and leave the decision where
it can be argued with.

The bar for proposing at all is deliberately high — 20 resolved outcomes before
an engine is judged, and a change has to survive its own confidence interval
rather than merely look better.

WHAT IT MEASURES THAT P&L CANNOT
--------------------------------
Every DETECTION is scored, including the refused ones. "PDL fired 25 times and
hit target zero" and "REJECTED_COST declined 10 setups, none of which worked"
are both statements no equity curve can make, and they are the two that decide
whether an engine stays and whether a gate is calibrated.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_float, get_supabase, today_ist

# An engine is not judged below this many resolved outcomes. 25 detections in a
# session looks like plenty and is one day of one market regime.
MIN_SAMPLE = 20

# A verdict also needs the sample to span real time, not one burst. 236
# detections from a single Tuesday is one market, one index path, one
# volatility regime — a large n that carries almost no independent
# information. 10 sessions is ~2 trading weeks, not "days" — a verdict, and
# especially a new candidate's first PROMOTE, needs weeks of data, not a
# lucky run inside one.
MIN_SESSIONS = 10


def dedupe_setups(rows: list) -> list:
    """
    One row per real setup, not one row per evaluation tick.

    THE MOST DANGEROUS THING IN THIS FILE BEFORE THIS FUNCTION EXISTED
    ------------------------------------------------------------------
    The engine evaluates every symbol every 15 seconds and records a detection
    each time the setup is still live. So a single setup on a single symbol
    writes a row over and over until it is taken, invalidated or the level moves
    away. Measured on the first 460 detections:

        460 rows  ->  97 distinct (session, symbol, engine) setups
        LALPATHLAB/RNG on 28 Jul alone:  52 rows
        RKFORGE/PDL:                     43 rows
        UNIONBANK/VWR:                   42 rows

    Every statistic in this review counted those as independent observations,
    and none of them are. The consequences were not subtle:

      · MIN_SAMPLE = 20 was satisfied by ONE symbol ticking twenty times. The
        guard that exists to stop a verdict being formed on a single day's
        market was being cleared by a single SYMBOL on a single day.
      · The tradeable population showed a 100% hit rate below 0.55 confidence
        over "15 outcomes". It was two setups — CONCOR detected fourteen times
        and GODFRYPHLP once — and it made the conviction floor look actively
        harmful on the strength of one name that happened to work.
      · VWR read 68% of 31 tradeable outcomes, comfortably a PROMOTE. Those 31
        rows are 11 real setups hitting 36%, which is not enough to promote
        anything.

    A repeated reading of one event is one observation. Keeping the FIRST
    detection is the honest choice: it is the one an entry would actually have
    been taken on, since the engine skips a symbol once it holds a position in
    it, and every later row describes a chance that was already spent.
    """
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (str(r.get("trade_date")), r.get("symbol"), r.get("strategy"))
        cur = best.get(key)
        if cur is None or str(r.get("ts") or "") < str(cur.get("ts") or ""):
            best[key] = r
    return list(best.values())


def _tradeable_floor() -> float:
    """
    The stop distance below which the cost model refuses a setup outright.

    is_worth_taking() rejects any setup whose stop is closer than
    cost / intraday_cost_stop_ratio. This reads the SAME two config values
    rather than restating the number, because a floor that is hardcoded here
    and computed there will disagree the first time either is tuned — and the
    whole point of this guardrail is that a threshold changed in one place must
    change how history is read everywhere.
    """
    from config import cfg_float
    from intraday.cost_model import round_trip
    ratio = cfg_float("intraday_cost_stop_ratio", 0.35)
    # Cost is essentially flat with size on this account (0.206% from ₹5k to
    # ₹20k), so a representative position gives the right floor.
    cost = round_trip(1000.0, 10).pct_of_position
    return cost / ratio if ratio > 0 else 0.0

# One id per pass, so every proposal from a single review can be read, accepted
# or rolled back together.
from datetime import datetime as _dt
_RUN_ID = f"weekly_{_dt.now():%Y%m%d_%H%M%S}"


def _hdr(t: str) -> None:
    logger.info("")
    logger.info("─" * 72)
    logger.info(t)
    logger.info("─" * 72)


def _propose(sb, kind: str, subject: str, current: str, suggested: str,
             evidence: str, confidence: str) -> None:
    """
    Record a proposal. Idempotent per (kind, subject, suggested) while OPEN, so
    a second run in the same week restates rather than duplicates — a review you
    cannot re-run is a review you run once and stop trusting.
    """
    # brain_proposals, not a new table. It already exists with 43 rows and
    # exactly this shape — target_key, current_value, proposed_value, evidence,
    # confidence, status — and a second table holding the same concept is how a
    # schema becomes unreadable. The only difference is `source`, which says
    # which analysis raised it.
    try:
        existing = (sb.table("brain_proposals").select("id")
                      .eq("proposal_type", kind).eq("target_key", subject)
                      .eq("proposed_value", suggested).eq("status", "PENDING")
                      .execute().data or [])
        # analysis_run_id is NOT NULL and groups every proposal from one pass,
        # matching the brain's own "brain_YYYYMMDD_HHMMSS" convention so both
        # sources read the same way in the table.
        row = {"analysis_run_id": _RUN_ID,
               "proposal_type": kind, "target_key": subject,
               "current_value": current, "proposed_value": suggested,
               "evidence": evidence, "rationale": evidence,
               "confidence": 0.8 if confidence == "high" else 0.5,
               "status": "PENDING", "source": "weekly_review",
               "priority": 1 if confidence == "high" else 2}
        if existing:
            sb.table("brain_proposals").update(row).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("brain_proposals").insert(row).execute()
    except Exception as e:
        logger.warning(f"  could not record proposal ({subject}): {e}")


def _supersede(sb, kind: str, subject: str, keep: str | None, why: str) -> int:
    """
    Retire PENDING proposals for this subject that this pass no longer stands by.

    WHY A LEARNING LOOP MUST BE ABLE TO WITHDRAW
    --------------------------------------------
    _propose() is idempotent per (kind, subject, proposed_value), so re-running a
    review restates a proposal rather than duplicating it. What it could not do
    is take one BACK. A proposal written last week under a configuration that has
    since changed stayed PENDING forever, sitting in the dashboard next to this
    week's contradicting one with nothing to say which was current.

    That is not tidiness, it is a live hazard. The first run of the guarded
    review put "PDL: widen the stop, the engine is not failing" directly beneath
    a stale "PDL: ACTIVE -> RETIRE" left by the unguarded version — two open
    recommendations about the same engine pointing in opposite directions, both
    marked PENDING, both looking equally authoritative. Acting on the wrong one
    retires a working engine.

    So every pass now withdraws what it no longer believes, with the reason
    recorded. SUPERSEDED, never deleted: the audit trail is the point, and a
    proposal that vanishes is one nobody can learn from.
    """
    try:
        q = (sb.table("brain_proposals").select("id,proposed_value")
               .eq("proposal_type", kind).eq("target_key", subject)
               .eq("status", "PENDING").eq("source", "weekly_review"))
        rows = q.execute().data or []
    except Exception as e:
        logger.debug(f"  could not scan proposals for {subject}: {e}")
        return 0
    n = 0
    for r in rows:
        if keep is not None and r.get("proposed_value") == keep:
            continue
        try:
            sb.table("brain_proposals").update({
                "status": "SUPERSEDED",
                "rationale": (f"withdrawn by {_RUN_ID}: {why}"),
            }).eq("id", r["id"]).execute()
            n += 1
            logger.info(f"      withdrew a stale proposal for {subject} "
                        f"({r.get('proposed_value')}) — {why}")
        except Exception as e:
            logger.debug(f"  could not supersede proposal {r['id']}: {e}")
    return n


def review_engines(sb, days: int = 30) -> list:
    """
    Score every intraday engine on resolved outcomes, and say what to do.

    JUDGED ONLY ON TRADES THE CURRENT CONFIGURATION WOULD STILL TAKE
    ----------------------------------------------------------------
    This is the guardrail that stops the learning loop from destroying working
    engines, and it was not hypothetical. Measured on the full 460-detection
    history the moment the outcome backfill made it visible:

        engine   ALL detections        below the cost floor    at/above it
        VWR      127, 27% hit          96, 14% hit             31, 68% hit
        PDL       94,  6% hit          94,  6% hit              0,  no sample
        RNG       61,  0% hit          61,  0% hit              0,  no sample
        VCE       56,  7% hit          39,  5% hit             17, 12% hit
        ORB       20,  0% hit           0                      20,  0% hit

    The cost model refuses any setup whose stop is closer than ~0.59% — a rail
    that landed on 31 July after measuring that a stop inside three times the
    round trip is not a stop but a fee. EVERY ONE of PDL's 94 detections and all
    61 of RNG's sit below that floor. They are trades the system can no longer
    take, and the naive scorecard would have proposed RETIRE for both on the
    strength of them: retiring two engines for a fault that had already been
    fixed, on evidence about trades that can never recur.

    The same blindness ran the other way and cost more. VWR reads 27% overall —
    "keep", unremarkable. On the 31 setups it can actually still take it hits
    68%, comfortably the best engine in the system. That is a PROMOTE hiding
    inside a shrug, and it stayed hidden because 96 sub-floor setups were
    averaged in with it.

    So the population is split, the verdict is formed on the tradeable half, and
    an engine whose entire sample is untradeable gets "re-measure" rather than a
    death sentence. Nothing here is thrown away — the excluded half is still
    printed, because "this engine only ever produces setups the cost model
    refuses" is itself a finding worth acting on, just not by retiring it.
    """
    _hdr(f"INTRADAY ENGINES — last {days} days of resolved outcomes")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups")
              # symbol and ts are REQUIRED by dedupe_setups — the key is
              # (session, symbol, engine) and the tie-break is the earliest
              # timestamp. Omitting symbol silently collapses every name on a
              # day into one row per engine, which understates the sample
              # instead of overstating it: 460 detections read as 19 setups
              # rather than the true 97. Both directions are wrong, and a
              # missing column produces neither an error nor a warning.
              .select("strategy,cost_verdict,outcome,outcome_pct,confidence,"
                      "trade_date,risk_pct,symbol,ts")
              .gte("trade_date", since).execute().data or [])
    raw_n = len([r for r in rows if r.get("outcome")])
    done = [r for r in dedupe_setups(rows) if r.get("outcome")]
    if not done:
        logger.info("  no resolved outcomes in the window")
        return []

    floor = _tradeable_floor()
    logger.info(f"  {raw_n} resolved detections collapse to {len(done)} distinct "
                f"setups — the engine re-records a live setup every 15s tick, and "
                f"counting those as separate observations is how one symbol clears "
                f"a {MIN_SAMPLE}-sample bar on its own")
    logger.info(f"  cost-model stop floor in force: {floor:.3f}% — setups below it "
                f"are excluded from the verdict, because the system would refuse "
                f"them today")

    def _risk(r) -> float:
        try:
            return float(r.get("risk_pct") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _tally(rs: list) -> dict:
        t = sum(1 for r in rs if r["outcome"] == "TARGET")
        pct = [float(r["outcome_pct"]) for r in rs if r.get("outcome_pct") is not None]
        return {"n": len(rs), "t": t,
                "hit": t / len(rs) if rs else 0.0,
                "avg": sum(pct) / len(pct) if pct else 0.0,
                "sessions": len({str(r.get("trade_date")) for r in rs})}

    by = defaultdict(list)
    for r in done:
        by[r.get("strategy") or "?"].append(r)

    cfg = {c["strategy"]: c for c in
           (sb.table("strategy_config").select("strategy,lifecycle,enabled")
              .execute().data or [])}

    out = []
    logger.info("")
    logger.info(f"  {'engine':<8}{'all':>6}{'hit':>6}  |{'tradeable':>10}{'hit':>6}"
                f"{'avg%':>8}{'days':>6}   lifecycle -> proposal")
    for eng, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        allt = _tally(rs)
        good = [r for r in rs if _risk(r) >= floor]
        excl = len(rs) - len(good)
        g = _tally(good)
        cur = (cfg.get(eng) or {}).get("lifecycle") or "ACTIVE"

        # ── the verdict, formed only on the tradeable population ────────────
        if g["n"] == 0:
            verdict = "hold"
            why = (f"all {allt['n']} detections had a stop tighter than the "
                   f"{floor:.2f}% cost floor and would be refused today — there is "
                   f"no evidence about what this engine does under the current "
                   f"configuration. Re-measure; do not retire on this")
        elif g["n"] < MIN_SAMPLE:
            verdict = "hold"
            why = (f"only {g['n']} tradeable outcomes ({excl} excluded below the "
                   f"{floor:.2f}% floor) — below the {MIN_SAMPLE} needed to judge")
        elif g["sessions"] < MIN_SESSIONS:
            verdict = "hold"
            why = (f"{g['n']} tradeable outcomes but from only {g['sessions']} "
                   f"session(s) — one day is one market, and a threshold moved on "
                   f"it would be fitted to that day rather than to the edge")
        elif g["hit"] < 0.10:
            verdict = "RETIRE"
            why = (f"{g['hit']:.0%} of {g['n']} tradeable setups reached target over "
                   f"{g['sessions']} sessions, avg {g['avg']:+.2f}%")
        elif g["hit"] < 0.25:
            verdict = "SHADOW"
            why = (f"{g['hit']:.0%} of {g['n']} tradeable — detects but does not "
                   f"deliver, avg {g['avg']:+.2f}%")
        elif g["hit"] >= 0.40 and g["avg"] > 0:
            verdict = "PROMOTE"
            why = (f"{g['hit']:.0%} of {g['n']} tradeable setups over "
                   f"{g['sessions']} sessions, avg {g['avg']:+.2f}%")
        else:
            verdict = "keep"
            why = f"{g['hit']:.0%} of {g['n']} tradeable, avg {g['avg']:+.2f}%"

        flag = ""
        if excl and allt["n"]:
            flag = f"  [{excl}/{allt['n']} below floor]"
        logger.info(f"  {eng:<8}{allt['n']:>6}{allt['hit']:>5.0%}  |{g['n']:>10}"
                    f"{g['hit']:>5.0%}{g['avg']:>8.2f}{g['sessions']:>6}   "
                    f"{cur} -> {verdict}{flag}")

        # An engine producing nothing but untradeable setups is a real problem —
        # it burns scan budget and teaches the loop nothing. Say so, as a
        # parameter proposal, which is what it actually is.
        if g["n"] == 0 and allt["n"] >= MIN_SAMPLE:
            _propose(sb, "ENGINE_PARAMETERS", eng, f"stops below {floor:.2f}%",
                     "widen the stop or the anchor",
                     (f"every one of {allt['n']} detections had a stop tighter than "
                      f"the {floor:.2f}% cost floor, so none could be taken. The "
                      f"engine is not failing, it is proposing trades the cost "
                      f"model correctly refuses — the stop placement is what needs "
                      f"changing, not the lifecycle"), "high")

        if verdict in ("RETIRE", "SHADOW", "PROMOTE") and verdict.lower() != cur.lower():
            _supersede(sb, "ENGINE_LIFECYCLE", eng, keep=verdict,
                       why=f"this pass proposes {verdict} instead")
            _propose(sb, "ENGINE_LIFECYCLE", eng, cur, verdict, why,
                     "high" if g["n"] >= MIN_SAMPLE * 2 else "medium")
            out.append((eng, verdict, why))
        else:
            # No lifecycle change is warranted. Anything still open proposing
            # one was written on evidence this pass has re-read and rejected —
            # most importantly the RETIRE verdicts the unguarded review produced
            # for engines whose entire sample sits below the cost floor.
            _supersede(sb, "ENGINE_LIFECYCLE", eng, keep=None,
                       why=(f"re-measured on the tradeable population: {why}"))
    return out


# Gates whose refusal is arithmetic rather than judgement, and which must
# therefore never be proposed for loosening no matter what the sample says.
#
# REJECTED_COST is the one that matters. It declines a setup when the target
# does not clear the round trip by the keep-ratio, or when the stop is inside
# ~3x the friction. Those are not opinions about the market that evidence can
# overturn — they are statements about ₹20 brokerage, 0.025% STT and stamp duty,
# and they are true whether or not a particular refused setup happened to reach
# its target. A refused setup that "would have worked" gross may still have lost
# money net, which is precisely the confusion the cost model exists to remove.
#
# Loosening it would also be self-justifying: admit tighter stops, and the newly
# admitted population reaches target more often per unit of stop distance while
# losing money after costs. The measured floor is the opposite lesson — setups
# at or above the floor hit 55%, those below it 8%.
_ARITHMETIC_GATES = {"REJECTED_COST"}


def review_gates(sb, days: int = 14) -> list:
    """
    Were the refusals right?

    A gate that declines setups which would have worked is costing money
    silently — there is no losing trade to notice, only the absence of a winning
    one. This is the only place that absence becomes visible.

    THREE GUARDRAILS, EACH FOR A DIFFERENT WAY THIS GOES WRONG
    ---------------------------------------------------------
    1. Compare like with like. The taken population is, by construction, the one
       that already cleared the cost floor; the refused population is mostly
       below it. Comparing the two raw is comparing a filtered sample against an
       unfiltered one, and the gap that produces is an artefact of the filter,
       not evidence about the gate.
    2. Never propose loosening an arithmetic gate. See _ARITHMETIC_GATES.
    3. Loosening is a ONE-WAY DOOR toward more risk and gets a higher bar than
       tightening: more sample, more sessions, and a margin wide enough that it
       is not a run of luck. Tightening costs opportunity, which is recoverable;
       loosening costs money, which is not.
    """
    _hdr(f"GATES — did refusing turn out to be correct? ({days}d)")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups")
              # symbol, strategy and ts are the dedupe key — see review_engines.
              .select("cost_verdict,outcome,trade_date,risk_pct,symbol,strategy,ts")
              .gte("trade_date", since).execute().data or [])
    # Same de-duplication as review_engines, for the same reason: a gate that
    # refused one setup forty times refused one setup.
    done = [r for r in dedupe_setups(rows) if r.get("outcome")]
    if not done:
        logger.info("  nothing resolved yet")
        return []

    floor = _tradeable_floor()

    def _risk(r) -> float:
        try:
            return float(r.get("risk_pct") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Guardrail 1: everything below is measured on the tradeable population only.
    tradeable = [r for r in done if _risk(r) >= floor]
    logger.info(f"  {len(tradeable)} of {len(done)} resolved setups clear the "
                f"{floor:.2f}% cost floor — the rest are excluded from both sides "
                f"of the comparison, since the system would refuse them today")
    if not tradeable:
        logger.warning("  no tradeable setups in the window — no gate can be judged")
        return []

    by = defaultdict(list)
    for r in tradeable:
        by[r.get("cost_verdict") or "?"].append(r)

    taken = by.get("TAKEN", [])
    base = (sum(1 for r in taken if r["outcome"] == "TARGET") / len(taken)) if taken else 0.0
    logger.info(f"  taken setups reach target {base:.0%} of the time (n={len(taken)}) "
                f"— the bar a refusal has to beat")

    out = []
    for v, rs in sorted(by.items()):
        if v == "TAKEN":
            continue
        n = len(rs)
        t = sum(1 for r in rs if r["outcome"] == "TARGET")
        hit = t / n if n else 0
        sessions = len({str(r.get("trade_date")) for r in rs})
        note = "  (arithmetic — never loosened)" if v in _ARITHMETIC_GATES else ""
        logger.info(f"    {v:<20} {t}/{n} would have worked ({hit:.0%}) "
                    f"over {sessions} session(s){note}")

        if v in _ARITHMETIC_GATES:
            continue                                    # guardrail 2
        # Guardrail 3: a one-way door needs a wider margin and a real sample.
        if (n >= MIN_SAMPLE * 2 and sessions >= MIN_SESSIONS
                and hit > max(base * 1.5, base + 0.10)):
            why = (f"refused {n} tradeable setups across {sessions} sessions, of "
                   f"which {hit:.0%} reached target against {base:.0%} for those "
                   f"taken — this gate is rejecting the good ones. Measured only "
                   f"on setups that clear the {floor:.2f}% cost floor, so the gap "
                   f"is not an artefact of stop distance")
            _propose(sb, "GATE_TOO_STRICT", v, "as configured", "loosen", why, "high")
            out.append((v, why))
        elif n >= MIN_SAMPLE and hit > base:
            logger.info(f"      ↑ refused better than taken, but {n} setups over "
                        f"{sessions} session(s) is short of the bar for loosening "
                        f"a gate ({MIN_SAMPLE * 2} over {MIN_SESSIONS}+ sessions, "
                        f"and >{max(base * 1.5, base + 0.10):.0%}). Watching")
    if not out:
        logger.info("  no gate cleared the bar for loosening — refusals are declining "
                    "worse setups than they allow, which is their job")
    return out


def review_ranking(sb, days: int = 30) -> None:
    """
    Did the entry ranking pick the right names?

    entry_rationale records the composite score each position was chosen on. If
    higher-ranked entries do not outperform lower-ranked ones, the weights are
    decoration — and that is testable rather than arguable.
    """
    _hdr(f"ENTRY RANKING — do higher-ranked picks do better? ({days}d)")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("closed_positions")
              .select("symbol,entry_rationale,r_multiple,realized_pnl,exit_date")
              .gte("exit_date", since).execute().data or [])
    scored = []
    have_rank = have_r = 0
    for r in rows:
        raw = r.get("entry_rationale") or ""
        if "rank" in raw:
            have_rank += 1
        if r.get("r_multiple") is not None:
            have_r += 1
        if "rank" not in raw or r.get("r_multiple") is None:
            continue
        try:
            scored.append((float(raw.split("rank", 1)[1].split("—")[0].strip()),
                           float(r["r_multiple"]), r["symbol"]))
        except Exception:
            continue
    if len(scored) < 6:
        # SAY WHICH KIND OF NOTHING THIS IS.
        #
        # "0 ranked trades closed — needs ~6" was printed every week for months
        # and read as patience. It was not: close_position() did not carry
        # entry_rationale into closed_positions at all, so the column was
        # structurally NULL and the count could never rise no matter how many
        # trades closed. Distinguishing "few trades" from "the inputs are not
        # being recorded" is the difference between waiting and being broken,
        # and only one of those is worth waiting for.
        logger.info(f"  {len(scored)} ranked trades closed of {len(rows)} in the "
                    f"window — needs ~6 before the comparison means anything")
        if rows and not have_rank:
            logger.warning(
                f"  none of the {len(rows)} closed trades carries an entry rank. "
                f"That is not a small sample, it is a missing input — check that "
                f"entry_rationale is written at entry AND carried through "
                f"close_position(). Until it is, this review can never produce a "
                f"verdict on the ranking weights.")
        elif rows and not have_r:
            logger.warning(
                f"  {have_rank} closed trades carry a rank but none carries an "
                f"r_multiple — planned_stop is missing at entry, so R cannot be "
                f"computed. The ranking cannot be scored without it.")
        return
    scored.sort(key=lambda x: -x[0])
    half = len(scored) // 2
    top = sum(x[1] for x in scored[:half]) / half
    bot = sum(x[1] for x in scored[half:]) / (len(scored) - half)
    logger.info(f"  top half by rank: {top:+.2f}R avg   bottom half: {bot:+.2f}R avg")
    if top <= bot:
        _propose(sb, "RANKING_INEFFECTIVE", "entry_ranking",
                 "current weights", "review weights",
                 f"higher-ranked entries averaged {top:+.2f}R against {bot:+.2f}R for "
                 f"lower-ranked ones over {len(scored)} trades — the ranking is not "
                 f"separating winners", "medium")
        logger.warning("  the ranking is NOT separating winners — proposal raised")
    else:
        logger.success(f"  ranking is separating winners by {top - bot:+.2f}R")


def review_ai_tier_weight(sb) -> None:
    """
    Is ai_tier ready to leave the 0.0 weight it has sat at since 04-Aug-2026?

    entry_ranking.score_plan()'s own comment states the exact unlock
    condition: tier-by-tier forward returns from resolved outcomes, once
    each tier clears a trustable sample — not a calendar date, not someone
    remembering to ask. Operator's own request, 07-Aug-2026: "ensure it gets
    picked at the right time in future." This is that check, run
    automatically every week the review runs, so the question is re-asked
    on its own schedule.

    Same query shape as allocation.scoring.tercile_report's ai_tier section
    (07-Aug-2026) — entered + resolved plans, R = outcome_return_pct /
    ((entry_zone_high - planned_stop) / entry_zone_high * 100). Reused, not
    re-derived, so the two can never quietly disagree about what "R" means.
    """
    _hdr("AI TIER WEIGHT — is rank_weight_tier ready to leave 0? (all resolved)")
    from allocation.scoring import _dist

    floor = 30   # Prior's own convention: "needs 30 observations to be trusted"
    rows, off = [], 0
    while True:
        page = (sb.table("signal_output_daily")
                  .select("outcome_return_pct,outcome_entered,"
                          "entry_zone_high,planned_stop,ai_tier")
                  .not_.is_("outcome_category", "null")
                  .range(off, off + 1000 - 1).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000

    by_tier: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if not r.get("outcome_entered"):
            continue
        entry, stop, ret = r.get("entry_zone_high"), r.get("planned_stop"), r.get("outcome_return_pct")
        if None in (entry, stop, ret):
            continue
        try:
            entry, stop = float(entry), float(stop)
            risk_pct = (entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            tier = str(r.get("ai_tier") or "UNTIERED").upper()
            by_tier[tier].append(float(ret) / risk_pct)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    needed = ("TIER_1", "TIER_2", "TIER_3")
    priors = {t: _dist(f"AI_TIER/{t}", by_tier.get(t, []), floor) for t in needed}
    for t in needed:
        p = priors[t]
        (logger.warning if p.below_floor else logger.info)(f"  {p.describe()}")

    still_thin = [t for t in needed if priors[t].below_floor]
    if still_thin:
        logger.info(f"  not ready — {', '.join(still_thin)} still below the "
                    f"{floor}-sample floor. Re-checked automatically next week; "
                    f"leaving the weight at 0 until then.")
        return

    t1, t2, t3 = priors["TIER_1"].mean_r, priors["TIER_2"].mean_r, priors["TIER_3"].mean_r
    min_sep = cfg_float("ai_tier_separation_min", 0.05)
    if t1 > t2 > t3 and (t1 - t3) >= min_sep:
        # PROPOSES THAT IT'S READY, NOT A SPECIFIC WEIGHT. How much to weight
        # a signal is a calibration question this measurement alone cannot
        # answer — proposing a precise number here would be the same
        # "unpriced risk" the original demotion warned about, one level up.
        _propose(sb, "AI_TIER_WEIGHT_READY", "rank_weight_tier",
                 "0.0 (demoted 04-Aug-2026, pending validation)",
                 "nonzero — needs calibration against this evidence",
                 f"all three tiers cleared the {floor}-sample floor with a "
                 f"monotonic separation: TIER_1 {t1:+.3f}R > TIER_2 {t2:+.3f}R > "
                 f"TIER_3 {t3:+.3f}R — the AI's tier now carries measurable "
                 f"signal on the full resolved record", "high")
        logger.success(f"  READY: monotonic separation confirmed "
                       f"(TIER_1 {t1:+.3f}R > TIER_2 {t2:+.3f}R > TIER_3 {t3:+.3f}R) "
                       f"— proposal raised in brain_proposals")
    else:
        logger.info(f"  sample clears the floor but shows no clean separation "
                    f"yet (TIER_1 {t1:+.3f}R, TIER_2 {t2:+.3f}R, TIER_3 {t3:+.3f}R, "
                    f"need >= {min_sep:.2f}R gap, monotonic) — leaving the "
                    f"weight at 0")


def show_open(sb) -> int:
    _hdr("OPEN PROPOSALS")
    try:
        rows = (sb.table("brain_proposals").select("*")
                  .eq("status", "PENDING").eq("source", "weekly_review")
                  .order("created_at", desc=True).execute().data or [])
    except Exception as e:
        logger.error(f"  brain_proposals unreadable — {e}")
        return 1
    if not rows:
        logger.success("  none — nothing the evidence supports changing")
        return 0
    for r in rows:
        logger.warning(f"  [{r.get('confidence')}] {r['proposal_type']} · "
                       f"{r['target_key']}: {r.get('current_value')} -> "
                       f"{r['proposed_value']}")
        logger.info(f"      {r.get('evidence')}")
    logger.info("")
    logger.info("  These are PROPOSALS. Nothing changes until you act on one.")
    return 0


def main(show: bool = False) -> int:
    sb = get_supabase()
    logger.info("═" * 72)
    logger.info("TradeOS — weekly learning review")
    logger.info("═" * 72)
    if show:
        return show_open(sb)

    review_engines(sb)
    review_gates(sb)
    review_ranking(sb)
    review_ai_tier_weight(sb)

    # Refresh the aggregates the dashboard reads. performance_metrics had not
    # been written since 2026-05-12, which is why the Engine Leaderboard said
    # "no engine stats yet" — the panel was correct and the data was absent.
    try:
        from swing.brain.performance_tracker import compute_daily_metrics
        compute_daily_metrics(sb, date.fromisoformat(str(today_ist())))
        logger.success("  performance_metrics refreshed")
    except Exception as e:
        logger.warning(f"  performance_metrics not refreshed — {e}")

    return show_open(sb)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Weekly learning review — proposes, never applies")
    ap.add_argument("--show", action="store_true", help="just read the open proposals")
    sys.exit(main(ap.parse_args().show))
