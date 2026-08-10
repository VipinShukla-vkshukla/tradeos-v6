"""
Would the new allocator have made money? Replayed against what actually happened.

    python -m tools.allocator_replay --days 10
    python -m tools.allocator_replay --days 10 --framework INTRADAY

READ-ONLY. Writes nothing, places nothing, changes no config. Safe mid-session.

WHY THIS TOOL CAN EXIST AT ALL
------------------------------
`outcomes.resolve_day` scores EVERY detection in `intraday_setups`, taken or
refused. That is this architecture's rarest asset: the realised outcome of the
trades the system did NOT take. So a policy change can be evaluated on the
basket it WOULD have selected, not merely on the basket the old policy did —
which is the difference between a backtest and a rationalisation.

WALK-FORWARD, NOT IN-SAMPLE — THIS IS THE WHOLE POINT
-----------------------------------------------------
For each session D, the priors and the bar are built ONLY from rows dated
strictly before D, then applied to D's detections. Scoring day D with a prior
that contains day D is exactly the self-reference defect this change set exists
to fix (a bucket crossing its sample floor on rows the allocator wrote that
same morning, 10-Aug-2026). A tool that measured the fix using the bug would
report whatever we hoped to see.

WHAT IT COMPARES
----------------
    OLD   prior over every resolved detection, keyed so only the pooled
          book-level entry was ever reachable, outcome_pct taken as-is
          (double-charged on gate-passed rows), no absolute floor
    NEW   prior over gate-passed detections, per-engine keys reachable,
          gross reconstruction, bar clamped at alloc_edge_absolute_floor

Both are driven through the REAL `scoring.score()` and the REAL `hurdle()`
arithmetic, not a reimplementation — a second copy of a decision is how this
project once had three R:R models give three answers for one stock.

WHAT THE NUMBERS MEAN, AND WHAT THEY DO NOT
-------------------------------------------
`realised R` sums `outcome_pct / risk_pct` over the setups each policy would
have taken, using each row's own recorded risk. It is gross of slippage beyond
the modelled round trip and assumes every selected setup filled at its planned
entry — the same assumption `intraday_setups` itself makes. Treat a difference
of a few tenths of an R over a handful of sessions as noise; this project's own
standard is >=20 resolved outcomes across >=10 sessions before a verdict.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_float, cfg_int, today_ist
from allocation import scoring as S
from intraday import direction as D

PAGE = 1000
# Intraday hold is always well under half a day, so score()'s own
# max(hold_days, 0.5) floor binds. Passing 0.5 reproduces the live divisor
# exactly rather than approximating it.
INTRADAY_HOLD_DAYS = 0.5


def _fetch_setups(sb, since: str) -> list[dict]:
    rows, off = [], 0
    while True:
        page = (sb.table("intraday_setups")
                .select("id,trade_date,symbol,strategy,entry,stop,target,direction,"
                        "outcome,outcome_pct,cost_verdict,cost_pct,meta")
                .gte("trade_date", since)
                .not_.is_("outcome_pct", "null")
                .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE
    return rows


def _dedupe_candidates(rows: list[dict]) -> list[dict]:
    """
    One row per (symbol, engine) PER SESSION — not one row per 15s cycle.

    WHY THIS EXISTS. A setup that lingers near its zone gets re-logged every
    cycle it is re-evaluated: this project's own dedup-inflation finding
    (migration 054, "10 symbols each logged 548-600 rows in a single session")
    is the SWING side of the identical mechanism. `_setup_is_new()` already
    stops a repeat TAKEN write, but ALLOCATOR_DECLINED legitimately recurs —
    a hovering setup can be declined a dozen times as price drifts past the
    0.35% dedup threshold each time.

    That is fatal to a RETROSPECTIVE ranking specifically, even though it is
    harmless to the live system. Live only ever gets ONE real shot at a given
    lingering setup — it triggers on some cycle or it never does. This tool
    gathers a whole session's rows and ranks them as if each were an
    independent opportunity; without collapsing duplicates, one real setup
    that lingered can occupy several of the top-`cap` slots at once, counting
    its single real outcome multiple times and crowding out setups that only
    ever got logged once. `08-05` in the first run of this tool had 502
    "candidates" against a ~40-symbol universe — that is duplicate near-miss
    rows, not 502 independent detections.

    RESOLUTION, PER (symbol, strategy, trade_date): if any row for the group
    was actually TAKEN, that row IS the trade — keep only it, since its
    entry/outcome are what a live position would have carried. Otherwise keep
    the row with the highest `id` (chronologically last), which is the most
    current picture of that setup's price and edge before the group's fate
    was decided.

    DELIBERATELY NOT APPLIED TO THE PRIOR-BUILDING POPULATION (`history` in
    `replay()`). `scoring.intraday_priors()` reads `intraday_setups` with no
    such dedup — the live prior is built on exactly this duplicated
    population, bugs and all. Deduping history here would compare the NEW
    policy against a cleaner population than production actually uses, which
    is a different, unfair kind of advantage in the other direction.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r.get("symbol"), r.get("strategy"))].append(r)

    out = []
    for _, grp in groups.items():
        taken = [g for g in grp if (g.get("cost_verdict") or "").upper() == "TAKEN"]
        out.append(taken[0] if taken else max(grp, key=lambda g: g.get("id") or 0))
    return out


def _r_of(row: dict, gross: bool) -> float | None:
    """Realised R for one resolved detection, from its own recorded risk."""
    try:
        entry, stop = float(row.get("entry") or 0), float(row.get("stop") or 0)
        d = D.normalise(row.get("direction"))
        risk = D.risk_per_share(entry, stop, d) if entry else 0.0
        risk_pct = risk / entry * 100.0 if risk else 0.0
        if risk_pct <= 0:
            return None
        pct = float(row["outcome_pct"])
        if gross:
            pct += float(row.get("cost_pct") or 0)
        return pct / risk_pct
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return None


def _new_priors(rows: list[dict], floor: int) -> dict[str, S.Prior]:
    """
    THE LIVE FUNCTION, on a walk-forward slice. Not a copy of it.

    The first version of this tool reimplemented `intraday_priors()` here. The
    prior-deduplication fix then landed in scoring.py and this tool's next run
    produced BYTE-IDENTICAL output — because it had never been executing that
    function at all, so the change it existed to measure was invisible to it.
    A tool that measures a change must run the changed code; this project's own
    rule about `decide()` and `evaluate_exit()` applies to measurement code
    exactly as it applies to trading code.

    `intraday_priors(sb=None, rows=...)` skips its own fetch and runs the
    identical arithmetic — gating, gross reconstruction, per-(symbol, engine,
    day) dedup, per-engine keys, the book-level fallbacks — over whatever
    population it is handed.
    """
    return S.intraday_priors(None, rows=rows)


def _legacy_priors(rows: list[dict], floor: int) -> dict[str, S.Prior]:
    """
    The pre-10-Aug baseline, FROZEN ON PURPOSE.

    This one IS a deliberate second implementation, and the only one in this
    file, because it must not track scoring.py: it reproduces the behaviour
    being replaced so the comparison has a fixed reference point. Its three
    defining defects, all now fixed upstream:

      · population = every resolved detection, not gate-passed ones
      · per-engine keys unreachable (dict keyed "ORB", lookup asks
        "INTRADAY/ORB"), so only the pooled book entries ever matched
      · outcome_pct used as-is, i.e. already net of the round trip, which
        score() then charges a second time
      · no per-(symbol, engine, day) dedup, so a lingering setup votes once
        per evaluation cycle
    """
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        rv = _r_of(r, gross=False)
        if rv is None:
            continue
        key = r.get("strategy") or "?"
        if D.is_short(D.normalise(r.get("direction"))):
            key = f"{key}/SHORT"
        by[key].append(rv)
    longs = [x for k, v in by.items() if not k.endswith("/SHORT") for x in v]
    shorts = [x for k, v in by.items() if k.endswith("/SHORT") for x in v]
    return {"INTRADAY/ALL": S._dist("INTRADAY/ALL", longs, floor),
            "INTRADAY/ALL/SHORT": S._dist("INTRADAY/ALL/SHORT", shorts, floor)}


def _lookup(priors: dict, source: str, is_short: bool, floor: int) -> S.Prior:
    """The allocator's own ladder: class, then book, then NEUTRAL."""
    def usable(k):
        g = priors.get(k)
        return g if g is not None and g.usable else None
    if is_short:
        return (usable(f"INTRADAY/{source}/SHORT") or usable("INTRADAY/ALL/SHORT")
                or S._dist("INTRADAY/NONE/SHORT", [], floor=10 ** 9))
    return (usable(f"INTRADAY/{source}") or usable("INTRADAY/ALL")
            or S._dist("INTRADAY/NONE", [], floor=10 ** 9))


def _bar(edges: list[float], pct: float, apply_floor: bool,
         floor_value: float) -> float:
    """The percentile bar, optionally clamped. Time/scarcity are deliberately
    held at their neutral value: this measures the POLICY change, and letting
    the bar drift with a simulated clock would mix in an effect the replay
    cannot observe (how many slots were free at each instant)."""
    if not edges:
        return float("-inf")
    s = sorted(edges)
    bar = s[min(int(pct * len(s)), len(s) - 1)]
    if apply_floor and bar < floor_value:
        return floor_value
    return bar


def replay(days: int, sb=None) -> int:
    sb = sb or get_supabase()
    floor_n = cfg_int("priors_min_sample_intraday", 30)
    hurdle_pct = cfg_float("alloc_hurdle_percentile", 0.75)
    floor_edge = cfg_float("alloc_edge_absolute_floor", 0.0)
    cap = cfg_int("intraday_max_new_per_day", 4)

    since = (today_ist() - timedelta(days=days + 120)).isoformat()
    rows = _fetch_setups(sb, since)
    if not rows:
        logger.error("  no resolved intraday_setups rows — nothing to replay")
        return 1

    sessions = sorted({r["trade_date"] for r in rows})
    window = sessions[-days:]
    logger.info(f"  {len(rows)} resolved detections across {len(sessions)} "
                f"session(s); replaying the last {len(window)}")

    tot = {"OLD": {"n": 0, "r": 0.0, "wins": 0},
           "NEW": {"n": 0, "r": 0.0, "wins": 0}}
    per_session = []

    for d in window:
        history = [r for r in rows if r["trade_date"] < d]
        todays = [r for r in rows if r["trade_date"] == d]
        # Only detections that actually reached the allocator are replayable —
        # a setup refused by the structure or event gate never became a
        # proposal, and crediting the allocator with declining it would be
        # measuring a decision it never made.
        raw_cands = [r for r in todays
                    if (r.get("cost_verdict") or "").upper()
                    in ("TAKEN", "ALLOCATOR_DECLINED")]
        cands = _dedupe_candidates(raw_cands)
        if not history or not cands:
            continue

        row = {}
        for label, gated, gross, use_floor in (("OLD", False, False, False),
                                               ("NEW", True, True, True)):
            priors = (_new_priors(history, floor_n) if gated
                      else _legacy_priors(history, floor_n))
            # The bar is a percentile of THIS session's scored arrivals, which
            # is what hurdle() does live (`allocation_decisions.edge`). The
            # walk-forward split is on the PRIOR, which is the thing that
            # carries information forward between sessions.
            hist_edges, scored = [], []
            for r in cands:
                is_sh = D.is_short(D.normalise(r.get("direction")))
                meta = r.get("meta") or {}
                if isinstance(meta, str):
                    try:
                        import json
                        meta = json.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}
                qty = int((meta or {}).get("qty") or 0) or 1
                pr = _lookup(priors, r.get("strategy") or "?", is_sh, floor_n)
                try:
                    sc = S.score(float(r["entry"]), float(r["stop"]),
                                 float(r["target"]), qty, "MIS", pr,
                                 INTRADAY_HOLD_DAYS,
                                 direction="SHORT" if is_sh else "LONG")
                except (TypeError, ValueError, KeyError):
                    continue
                if sc.get("edge") is None:
                    continue
                scored.append((sc["edge"], r))
                hist_edges.append(sc["edge"])

            bar = _bar(hist_edges, hurdle_pct, use_floor, floor_edge)
            takes = sorted([s for s in scored if s[0] > bar],
                           key=lambda x: -x[0])[:cap]
            realised = [(_r_of(r, gross=False) or 0.0) for _, r in takes]
            row[label] = {"bar": bar, "n": len(takes),
                          "r": sum(realised),
                          "wins": sum(1 for x in realised if x > 0)}
            tot[label]["n"] += len(takes)
            tot[label]["r"] += sum(realised)
            tot[label]["wins"] += sum(1 for x in realised if x > 0)
        per_session.append((d, len(raw_cands), len(cands), row))

    if not per_session:
        logger.error("  no session had both prior history and allocator-visible "
                     "candidates — widen --days")
        return 1

    logger.info("")
    logger.info("  " + "-" * 88)
    logger.info(f"  {'session':<12}{'raw':>6}{'dedup':>7}"
                f"{'OLD bar':>10}{'OLD n':>7}{'OLD R':>8}"
                f"{'NEW bar':>10}{'NEW n':>7}{'NEW R':>8}")
    logger.info("  " + "-" * 88)
    for d, raw_n, nc, row in per_session:
        o, n = row["OLD"], row["NEW"]
        flag = "  <- duplicate-inflated" if raw_n > nc * 3 else ""
        logger.info(f"  {d:<12}{raw_n:>6}{nc:>7}"
                    f"{o['bar']:>10.4f}{o['n']:>7}{o['r']:>8.2f}"
                    f"{n['bar']:>10.4f}{n['n']:>7}{n['r']:>8.2f}{flag}")
    logger.info("  " + "-" * 88)

    o, n = tot["OLD"], tot["NEW"]
    for label, t in (("OLD", o), ("NEW", n)):
        hit = (t["wins"] / t["n"] * 100) if t["n"] else 0.0
        avg = (t["r"] / t["n"]) if t["n"] else 0.0
        logger.info(f"  {label}: {t['n']} trade(s), total {t['r']:+.2f}R, "
                    f"avg {avg:+.3f}R, {hit:.0f}% positive")

    delta = n["r"] - o["r"]
    logger.info("")
    if n["n"] == 0 and o["n"] == 0:
        logger.warning("  BOTH policies took zero trades — this window cannot "
                       "separate them. Widen --days.")
        return 0
    if n["n"] == 0:
        logger.warning(
            f"  THE NEW POLICY TOOK NOTHING while the old took {o['n']} for "
            f"{o['r']:+.2f}R. If that old total is NEGATIVE this is the floor "
            f"working as designed — refusing a losing book is the point. If it "
            f"is POSITIVE, the floor is too tight for this population and the "
            f"prior is what to investigate before enabling it live.")
        return 0
    verdict = ("IMPROVES" if delta > 0 else "DETERIORATES" if delta < 0 else "is NEUTRAL")
    fn = logger.success if delta > 0 else logger.warning
    fn(f"  NEW {verdict} on realised R by {delta:+.2f}R over "
       f"{len(per_session)} session(s) ({o['n']} -> {n['n']} trades).")
    if len(per_session) < cfg_int("replay_min_sessions", 10) or n["n"] < 20:
        logger.warning(
            f"  UNDERPOWERED: {len(per_session)} session(s), {n['n']} new-policy "
            f"trade(s). This project's own standard is >=20 outcomes across >=10 "
            f"sessions before a verdict. Read this as a direction, not a result.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Replay the allocator policy change "
                                             "against resolved outcomes (read-only)")
    ap.add_argument("--days", type=int, default=10,
                    help="how many recent sessions to replay (default 10)")
    sys.exit(replay(ap.parse_args().days))
