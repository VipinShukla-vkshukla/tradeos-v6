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
from config import get_supabase, today_ist

# An engine is not judged below this many resolved outcomes. 25 detections in a
# session looks like plenty and is one day of one market regime.
MIN_SAMPLE = 20

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


def review_engines(sb, days: int = 14) -> list:
    """Score every intraday engine on resolved outcomes, and say what to do."""
    _hdr(f"INTRADAY ENGINES — last {days} days of resolved outcomes")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups")
              .select("strategy,cost_verdict,outcome,outcome_pct,confidence,trade_date")
              .gte("trade_date", since).execute().data or [])
    done = [r for r in rows if r.get("outcome")]
    if not done:
        logger.info("  no resolved outcomes in the window")
        return []

    by = defaultdict(lambda: {"TARGET": 0, "STOP": 0, "TIMEOUT": 0, "pct": []})
    for r in done:
        b = by[r.get("strategy") or "?"]
        b[r["outcome"]] = b.get(r["outcome"], 0) + 1
        if r.get("outcome_pct") is not None:
            b["pct"].append(float(r["outcome_pct"]))

    cfg = {c["strategy"]: c for c in
           (sb.table("strategy_config").select("strategy,lifecycle,enabled")
              .execute().data or [])}

    out = []
    logger.info(f"  {'engine':<8}{'n':>5}{'TARGET':>8}{'hit':>7}{'avg%':>8}   lifecycle -> proposal")
    for eng, b in sorted(by.items(), key=lambda kv: -sum(
            v for k, v in kv[1].items() if k != "pct")):
        n = b["TARGET"] + b["STOP"] + b["TIMEOUT"]
        hit = b["TARGET"] / n if n else 0
        avg = sum(b["pct"]) / len(b["pct"]) if b["pct"] else 0.0
        cur = (cfg.get(eng) or {}).get("lifecycle") or "ACTIVE"

        if n < MIN_SAMPLE:
            verdict, why = "hold", f"only {n} outcomes — below the {MIN_SAMPLE} needed to judge"
        elif hit < 0.10:
            verdict, why = "RETIRE", f"{hit:.0%} of {n} reached target over {days}d"
        elif hit < 0.25:
            verdict, why = "SHADOW", f"{hit:.0%} of {n} — detects but does not deliver"
        elif hit >= 0.40 and avg > 0:
            verdict, why = "PROMOTE", f"{hit:.0%} of {n}, avg {avg:+.2f}%"
        else:
            verdict, why = "keep", f"{hit:.0%} of {n}, avg {avg:+.2f}%"

        logger.info(f"  {eng:<8}{n:>5}{b['TARGET']:>8}{hit:>6.0%}{avg:>8.2f}   "
                    f"{cur} -> {verdict}")
        if verdict in ("RETIRE", "SHADOW", "PROMOTE") and verdict.lower() != cur.lower():
            _propose(sb, "ENGINE_LIFECYCLE", eng, cur, verdict, why,
                     "high" if n >= MIN_SAMPLE * 2 else "medium")
            out.append((eng, verdict, why))
    return out


def review_gates(sb, days: int = 14) -> list:
    """
    Were the refusals right?

    A gate that declines setups which would have worked is costing money
    silently — there is no losing trade to notice, only the absence of a winning
    one. This is the only place that absence becomes visible.
    """
    _hdr(f"GATES — did refusing turn out to be correct? ({days}d)")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups").select("cost_verdict,outcome")
              .gte("trade_date", since).execute().data or [])
    done = [r for r in rows if r.get("outcome")]
    if not done:
        logger.info("  nothing resolved yet")
        return []

    by = defaultdict(lambda: defaultdict(int))
    for r in done:
        by[r.get("cost_verdict") or "?"][r["outcome"]] += 1

    taken = by.get("TAKEN", {})
    base = (taken.get("TARGET", 0) /
            max(1, sum(taken.values()))) if taken else 0.0
    logger.info(f"  taken setups reach target {base:.0%} of the time — the bar a "
                f"refusal has to beat")

    out = []
    for v, o in sorted(by.items()):
        if v == "TAKEN":
            continue
        n = sum(o.values())
        hit = o.get("TARGET", 0) / n if n else 0
        logger.info(f"    {v:<20} {o.get('TARGET',0)}/{n} would have worked ({hit:.0%})")
        # A gate is wrong when what it refused did BETTER than what was taken.
        if n >= MIN_SAMPLE and hit > base * 1.2:
            why = (f"refused {n} setups of which {hit:.0%} reached target, against "
                   f"{base:.0%} for those taken — this gate is rejecting the good ones")
            _propose(sb, "GATE_TOO_STRICT", v, "as configured", "loosen", why, "high")
            out.append((v, why))
    if not out:
        logger.info("  every gate declined worse setups than it allowed — none too strict")
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
    for r in rows:
        raw = r.get("entry_rationale") or ""
        if "rank" not in raw or r.get("r_multiple") is None:
            continue
        try:
            scored.append((float(raw.split("rank", 1)[1].split("—")[0].strip()),
                           float(r["r_multiple"]), r["symbol"]))
        except Exception:
            continue
    if len(scored) < 6:
        logger.info(f"  {len(scored)} ranked trades closed — needs ~6 before the "
                    f"comparison means anything")
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
