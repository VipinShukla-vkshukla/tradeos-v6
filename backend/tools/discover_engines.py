"""
Find edges nobody has coded yet.

    python -m tools.discover_engines            both passes
    python -m tools.discover_engines --days 10  wider window

WHY THE WEEKLY REVIEW CANNOT DO THIS
------------------------------------
weekly_review.py scores the seven engines that exist. It can retire one and it
can loosen a gate, but it cannot propose a strategy nobody wrote — it only ever
looks where the engines already look. That makes it structurally blind to the
thing worth the most: a move the system did not see at all.

TWO PASSES, TWO DIFFERENT BLIND SPOTS

  A. REFUSED BUT RIGHT.  Every setup a gate declined is recorded with its
     outcome. If refusals in some slice reach target MORE often than the setups
     actually taken, that slice is not a mis-tuned gate — it is an unnamed edge
     living in the rejected population. Cheap: the data and the comparison
     already exist, only the segmentation was missing.

  B. MOVED BUT UNSEEN.  Sweep the universe for stocks that made a real intraday
     move and ask which produced NO detection from ANY engine. Those are the
     misses that cost nothing visible and never appear in any P&L — there is no
     losing trade to notice, only a winner that never happened. Characterise
     what preceded them and look for a repeat.

WHAT A FINDING IS AND IS NOT
----------------------------
A pattern here is a HYPOTHESIS, never a strategy. It is proposed at SHADOW —
detected and scored, never traded — and must clear the same 20-outcome bar every
existing engine clears before it can be promoted. A system that invents engines
and trades them is one nobody can audit, which is the opposite of what this
project has spent its time building.

The bar for reporting at all is deliberately high. Given enough slices, noise
produces a "pattern" every time; MIN_OCCURRENCES and the lift threshold exist to
make sure a finding has to be surprising, not merely present.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_float, get_supabase, today_ist

# A cluster must recur before it is a pattern. Below this it is an anecdote, and
# anecdotes are what backtests are made of.
MIN_OCCURRENCES = 6

# How much better than the taken-setup baseline a refused slice must do before
# it is worth naming. 1.5x, not 1.05x — with enough slices something always
# looks slightly better.
MIN_LIFT = 1.5

# What counts as a move worth having caught, net of a 0.21% round trip.
MOVE_PCT = 1.5


def _hdr(t: str) -> None:
    logger.info("")
    logger.info("─" * 72)
    logger.info(t)
    logger.info("─" * 72)


def _propose(sb, subject: str, evidence: str, confidence: float) -> None:
    """
    Raise a SHADOW candidate. Same table as every other proposal, so a discovery
    is reviewed exactly like a retirement rather than through a side channel.
    """
    from tools.weekly_review import _RUN_ID
    try:
        existing = (sb.table("brain_proposals").select("id")
                      .eq("proposal_type", "ENGINE_CANDIDATE")
                      .eq("target_key", subject).eq("status", "PENDING")
                      .execute().data or [])
        row = {"analysis_run_id": _RUN_ID, "proposal_type": "ENGINE_CANDIDATE",
               "target_key": subject, "current_value": "does not exist",
               "proposed_value": "build as SHADOW", "evidence": evidence,
               "rationale": evidence, "confidence": confidence,
               "status": "PENDING", "source": "discover_engines", "priority": 3}
        if existing:
            sb.table("brain_proposals").update(row).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("brain_proposals").insert(row).execute()
    except Exception as e:
        logger.warning(f"  could not record candidate ({subject}): {e}")


# ── Pass A ──────────────────────────────────────────────────────────────────
def refused_but_right(sb, days: int) -> int:
    """
    Slices of the REFUSED population that outperformed the taken one.

    Segmented by (gate, strategy) and by (gate, session phase), because a gate
    is rarely wrong everywhere — it is wrong in a corner. "Structure blocks PDL
    during PRIME" is a sentence you can build an engine from; "structure is too
    strict" is not.
    """
    _hdr(f"A · REFUSED BUT RIGHT — gates that decline winners ({days}d)")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups")
              .select("strategy,cost_verdict,outcome,outcome_pct,phase,confidence")
              .gte("trade_date", since).execute().data or [])
    done = [r for r in rows if r.get("outcome")]
    if not done:
        logger.info("  nothing resolved in the window")
        return 0

    taken = [r for r in done if r.get("cost_verdict") == "TAKEN"]
    if not taken:
        logger.info("  no taken setups to form a baseline against")
        return 0
    base = sum(1 for r in taken if r["outcome"] == "TARGET") / len(taken)
    logger.info(f"  baseline: taken setups reach target {base:.0%} "
                f"(n={len(taken)}) — a refused slice must beat {base * MIN_LIFT:.0%}")

    slices = defaultdict(list)
    for r in done:
        v = r.get("cost_verdict")
        if not v or v == "TAKEN":
            continue
        slices[(v, r.get("strategy") or "?")].append(r)
        slices[(v, f"phase:{r.get('phase') or '?'}")].append(r)

    found = 0
    for (gate, seg), rs in sorted(slices.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        if n < MIN_OCCURRENCES:
            continue
        hit = sum(1 for r in rs if r["outcome"] == "TARGET") / n
        if hit <= base * MIN_LIFT:
            continue
        found += 1
        subject = f"{gate}/{seg}"
        why = (f"{gate} refused {n} setups in {seg} and {hit:.0%} reached target, "
               f"against {base:.0%} for setups actually taken — the refused "
               f"population in this slice is BETTER than the traded one")
        logger.warning(f"  ! {subject:<34} {hit:.0%} of {n}  (baseline {base:.0%})")
        logger.info(f"      {why}")
        _propose(sb, subject, why, 0.6 if n >= MIN_OCCURRENCES * 2 else 0.4)

    if not found:
        logger.success("  no refused slice beats the taken baseline — the gates are "
                       "declining worse setups than they allow, which is their job")
    return found


# ── Pass B ──────────────────────────────────────────────────────────────────
def moved_but_unseen(sb, days: int) -> int:
    """
    Real intraday moves that produced no detection from any engine.

    These are the expensive misses precisely because they are invisible: no
    losing trade, no bad exit, nothing in the P&L — only a winner that never
    happened. Nothing else in this system looks for them.

    Characterised on features available BEFORE the move, because a pattern
    described by what happened afterwards is not a signal, it is a description.
    """
    _hdr(f"B · MOVED BUT UNSEEN — real moves no engine detected ({days}d)")

    # stock_data_daily, not the broker.
    #
    # The swing pipeline already ingests OHLC for the whole universe every
    # evening. Re-fetching it from kite.historical_data would mean one
    # rate-limited call per symbol per day for data this database already holds,
    # and would make discovery depend on a live broker session for a question
    # about the past. A second source of truth for the same bars is also how
    # two parts of this project came to disagree about the same number.
    since = (today_ist() - timedelta(days=days)).isoformat()
    bars = (sb.table("stock_data_daily")
              .select("symbol,date,open,high,low,close,volume")
              .gte("date", since).execute().data or [])
    if not bars:
        logger.warning("  no bars in stock_data_daily for the window")
        return 0

    seen = defaultdict(set)      # trade_date -> symbols any engine detected
    for r in (sb.table("intraday_setups").select("symbol,trade_date")
                .gte("trade_date", since).execute().data or []):
        seen[str(r.get("trade_date"))].add(r.get("symbol"))

    from intraday import scanner
    universe = set(scanner.symbols(sb))

    # Previous close per symbol, so the gap is knowable at the open.
    by_sym = defaultdict(list)
    for b in bars:
        by_sym[b["symbol"]].append(b)
    for v in by_sym.values():
        v.sort(key=lambda x: str(x["date"]))

    misses, examined = [], 0
    for sym, rows in by_sym.items():
        # Only the intraday universe: a name that fails the liquidity or
        # movement screen is not a miss, it is correctly out of scope.
        if sym not in universe:
            continue
        for i, b in enumerate(rows):
            try:
                o, h, c = float(b["open"] or 0), float(b["high"] or 0), float(b["close"] or 0)
            except (TypeError, ValueError):
                continue
            if not o or not h:
                continue
            examined += 1
            up = (h - o) / o * 100.0
            if up < MOVE_PCT:
                continue
            d = str(b["date"])[:10]
            if sym in seen.get(d, set()):
                continue          # an engine saw it; not a miss
            prev_c = float(rows[i - 1]["close"] or 0) if i else 0.0
            gap = ((o - prev_c) / prev_c * 100.0) if prev_c else 0.0
            misses.append({"symbol": sym, "date": d, "move": round(up, 2),
                           "gap": round(gap, 2), "closed_strong": c > o})

    if not examined:
        logger.warning("  no usable bars — cannot judge")
        return 0
    logger.info(f"  {len(misses)} moves over {MOVE_PCT}% went undetected, "
                f"from {examined} symbol-days across {len(universe)} universe names")
    if not misses:
        logger.success("  every qualifying move was seen by some engine")
        return 0

    # Cluster on the one feature knowable in advance: the opening gap.
    buckets = {
        "gap down > 1%":       lambda m: m["gap"] <= -1.0,
        "gap down 0 to 1%":    lambda m: -1.0 < m["gap"] < 0,
        "flat open +/-0.3%":   lambda m: abs(m["gap"]) <= 0.3,
        "gap up 0.3 to 1.5%":  lambda m: 0.3 < m["gap"] <= 1.5,
        "gap up > 1.5%":       lambda m: m["gap"] > 1.5,
    }
    found = 0
    for name, fn in buckets.items():
        hits = [m for m in misses if fn(m)]
        if len(hits) < MIN_OCCURRENCES:
            continue
        strong = sum(1 for m in hits if m["closed_strong"]) / len(hits)
        avg = sum(m["move"] for m in hits) / len(hits)
        found += 1
        why = (f"{len(hits)} undetected moves averaging {avg:.2f}% opened with a "
               f"{name}; {strong:.0%} closed above the open. No engine fires on "
               f"this shape — it is uncovered ground, not a mis-tuned gate")
        logger.warning(f"  ! undetected · {name:<22} n={len(hits):<4} "
                       f"avg {avg:.2f}%  {strong:.0%} closed strong")
        logger.info("      e.g. " + ", ".join(
            f"{m['symbol']} {m['date']} +{m['move']:.1f}%" for m in hits[:3]))
        _propose(sb, f"UNSEEN/{name}", why,
                 0.6 if len(hits) >= MIN_OCCURRENCES * 2 else 0.4)
    if not found:
        logger.info("  misses do not cluster on any single opening shape — "
                    "no candidate worth naming yet")
    return found


def main(days: int = 14, skip_bars: bool = False) -> int:
    sb = get_supabase()
    logger.info("═" * 72)
    logger.info("TradeOS — engine discovery (proposes SHADOW candidates only)")
    logger.info("═" * 72)
    n = refused_but_right(sb, days)
    if not skip_bars:
        n += moved_but_unseen(sb, days)
    logger.info("")
    if n:
        logger.warning(f"  {n} candidate(s) raised — read them with `tradeos learn show`")
        logger.info("  Each is a HYPOTHESIS. Build as SHADOW, score 20 outcomes, "
                    "then decide.")
    else:
        logger.success("  no candidates — nothing in the data is asking for a new engine")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discover engines that do not exist yet")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--skip-bars", action="store_true",
                    help="pass A only; skips the historical sweep")
    a = ap.parse_args()
    sys.exit(main(a.days, a.skip_bars))
