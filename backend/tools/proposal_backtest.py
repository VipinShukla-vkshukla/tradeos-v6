"""
Give every PENDING brain_proposals row a walk-forward backtest, and write
the result into `backtest_result` — the column that has existed on this
table since it was created and, confirmed by direct query on 11-Aug-2026,
has never once been populated: 0 of 54 rows carry anything there.

    python -m tools.proposal_backtest                 # all PENDING, write results
    python -m tools.proposal_backtest --dry-run        # compute, print, write nothing
    python -m tools.proposal_backtest --id 189         # one specific proposal
    python -m tools.proposal_backtest --days 30        # walk-forward window

READ side-effect only: writes `backtest_result`, nothing else. NEVER
touches `status`, `applied_at`, or any config. Deciding APPROVED/REJECTED
stays a human call — same "propose, never auto-apply" rule every other
learning tool in this project follows; this only gives that decision the
evidence the schema was built to carry.

WHAT "BACKTESTABLE" ACTUALLY MEANS HERE — VERIFIED, NOT ASSUMED
------------------------------------------------------------------
Queried the real table before designing this (11-Aug-2026, 54 rows, 8
proposal_type values). Only ONE shape is mechanically testable without a
human first translating prose into a concrete change:

    target_key       a real system_config key
    current_value    parses as a float
    proposed_value   parses as a float, and differs from current_value

That covers `SWING_RESERVATION_INERT` (target_key=alloc_reserve_edge_
multiple, current=1.15) and any future proposal shaped the same way. It
does NOT cover, and this tool marks NOT_TESTABLE rather than silently
skipping:

    CODE_SUGGESTION (43 of 54 rows)   a bug report, not a trading
                                       parameter — needs code review, not
                                       a backtest
    ENGINE_CANDIDATE                  proposes a pattern with NO engine
                                       built yet — nothing exists to replay
    ENGINE_LIFECYCLE                  target_key is an engine NAME
                                       (ACTIVE/SHADOW/RETIRE/PROMOTE), not
                                       a system_config key — a different,
                                       harder replay this tool does not
                                       attempt yet (see the module docstring
                                       note below)
    ENGINE_PARAMETERS                 proposed_value is prose ("widen the
                                       stop or the anchor"), not a literal

A proposal this tool cannot test is still a proposal — the record should
say so, not stay permanently blank the way it has since the table existed.

THE METHOD, FOR WHAT IS TESTABLE
------------------------------------
Walks forward through resolved intraday_setups using the SAME population
(_fetch_setups), SAME dedup (_dedupe_candidates), SAME prior function
(scoring.intraday_priors, via allocator_replay._new_priors — "a tool that
measures a change must run the changed code", this project's own rule,
verified the hard way once already this session), SAME bar (_bar), SAME
lookup ladder (_lookup) as tools.allocator_replay — imported, not
reimplemented. The only thing that varies between the two passes is the
ONE config key the proposal names, held at CURRENT for one pass and
PROPOSED for the other via _override_one() — every other key stays at its
real production value, because comparing against a hypothetical rest-of-
config would answer a different question than "what would this ONE change
have done, today, with everything else as it actually is."

_override_one() is deliberately NOT tests.cfg_ctx: cfg_ctx REPLACES the
whole config, correct for a hermetic unit test and wrong here — a backtest
needs the real production values for every key except the one being
tested.

ENGINE_LIFECYCLE IS DELIBERATELY OUT OF SCOPE FOR NOW. Testing "what if GAP
had been SHADOW instead of ACTIVE for this window" means excluding GAP's
TAKEN rows from the admitted population and re-deriving what the freed
budget would have taken instead — a materially different replay shape than
a scalar config swap, and building it care lessly risks exactly the kind of
subtly-wrong second implementation this project has been burned by
repeatedly. Left NOT_TESTABLE with that reason recorded rather than
approximated.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_float, cfg_int, today_ist
from allocation import scoring as S
from intraday import direction as D
from tools.allocator_replay import (
    _fetch_setups, _dedupe_candidates, _new_priors, _lookup, _bar, _r_of,
    INTRADAY_HOLD_DAYS,
)

CONFIG_VALUE = "CONFIG_VALUE"
NOT_TESTABLE = "NOT_TESTABLE"

# proposal_type values confirmed by direct query NOT to carry a real
# system_config key in target_key, or not to carry a literal proposed
# value — see the module docstring for what each actually contains.
_NEVER_TESTABLE_TYPES = {"CODE_SUGGESTION", "ENGINE_CANDIDATE",
                         "ENGINE_LIFECYCLE", "ENGINE_PARAMETERS"}


@contextmanager
def _override_one(key: str, value: str):
    """
    Run with exactly ONE system_config key overridden, everything else at
    its real current value — the opposite of tests.cfg_ctx, which replaces
    the whole config for test isolation. A backtest needs the REAL
    production config for every key except the one proposal being tested,
    or the comparison is between two hypothetical systems, not "today,
    with one value changed."
    """
    import config
    config.get_system_config()  # ensure _sys_config is loaded before snapshotting
    saved = dict(config._sys_config or {})
    try:
        config._sys_config = {**saved, key: str(value)}
        yield
    finally:
        config._sys_config = saved


def _as_float(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def classify_proposal(row: dict) -> tuple[str, str]:
    """
    PURE. Returns (kind, reason) — kind is CONFIG_VALUE or NOT_TESTABLE.

    Does not check whether target_key EXISTS in system_config (a proposal
    for a not-yet-registered key is exactly the "silent default" case
    CLAUDE.md names as this project's dominant failure mode, and refusing
    to test it would hide rather than surface that). It only checks that
    both values PARSE — a real key check happens where the value is
    actually read, at backtest time, and its absence there is reported
    honestly rather than pre-filtered here.
    """
    ptype = (row.get("proposal_type") or "").upper()
    if ptype in _NEVER_TESTABLE_TYPES:
        return NOT_TESTABLE, f"{ptype} proposals do not name a system_config value"

    key = row.get("target_key") or ""
    if not key or key.startswith("code_suggestion:") or key.startswith("UNSEEN/"):
        return NOT_TESTABLE, f"target_key '{key}' is not a system_config key"

    cur = _as_float(row.get("current_value"))
    prop = _as_float(row.get("proposed_value"))
    if cur is None or prop is None:
        return NOT_TESTABLE, (
            f"current_value/proposed_value are not both literal numbers "
            f"(current={row.get('current_value')!r}, "
            f"proposed={row.get('proposed_value')!r}) — needs a human to "
            f"translate this into a concrete value before it is testable")
    if cur == prop:
        return NOT_TESTABLE, "current_value and proposed_value are identical"
    return CONFIG_VALUE, f"{key}: {cur} -> {prop}"


def _walk_forward(rows: list[dict], days: int, hurdle_pct: float,
                  floor_edge: float, floor_n: int, cap: int) -> dict:
    """
    THE 'NEW' COLUMN OF tools.allocator_replay.replay(), EXTRACTED — same
    population, same prior function, same bar, same lookup, imported not
    reimplemented (see this module's own docstring for why that rule
    matters here specifically). Reads whatever is currently in
    config._sys_config at call time for everything _new_priors()/S.score()
    themselves read, which is what makes wrapping a call in _override_one()
    meaningful.

    Returns {"n", "r", "wins", "sessions"} — sessions is how many days
    actually had both prior history and a candidate, the same
    underpowered-window signal replay() itself surfaces.
    """
    sessions = sorted({r["trade_date"] for r in rows})
    window = sessions[-days:]
    tot = {"n": 0, "r": 0.0, "wins": 0, "sessions": 0}

    for d in window:
        history = [r for r in rows if r["trade_date"] < d]
        todays = [r for r in rows if r["trade_date"] == d]
        raw_cands = [r for r in todays
                    if (r.get("cost_verdict") or "").upper()
                    in ("TAKEN", "ALLOCATOR_DECLINED")]
        cands = _dedupe_candidates(raw_cands)
        if not history or not cands:
            continue

        priors = _new_priors(history, floor_n)
        hist_edges, scored = [], []
        for r in cands:
            is_sh = D.is_short(D.normalise(r.get("direction")))
            meta = r.get("meta") or {}
            if isinstance(meta, str):
                try:
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

        bar = _bar(hist_edges, hurdle_pct, True, floor_edge)
        takes = sorted([s for s in scored if s[0] > bar], key=lambda x: -x[0])[:cap]
        realised = [(_r_of(r, gross=False) or 0.0) for _, r in takes]
        tot["n"] += len(takes)
        tot["r"] += sum(realised)
        tot["wins"] += sum(1 for x in realised if x > 0)
        tot["sessions"] += 1

    return tot


def backtest_config_proposal(row: dict, rows: list[dict], days: int,
                             hurdle_pct: float, floor_edge: float,
                             floor_n: int, cap: int) -> dict:
    """Runs _walk_forward() twice — current value, then proposed — and
    returns a structured comparison. The only difference between the two
    calls is what _override_one() has patched into config._sys_config."""
    key = row["target_key"]
    current = row.get("current_value")
    proposed = row.get("proposed_value")

    with _override_one(key, current):
        before = _walk_forward(rows, days, hurdle_pct, floor_edge, floor_n, cap)
    with _override_one(key, proposed):
        after = _walk_forward(rows, days, hurdle_pct, floor_edge, floor_n, cap)

    delta = after["r"] - before["r"]
    underpowered = (before["sessions"] < cfg_int("replay_min_sessions", 10)
                    or after["n"] < 20 or before["n"] < 20)
    if before["n"] == 0 and after["n"] == 0:
        verdict = "INCONCLUSIVE — both values took zero trades in this window"
    elif after["n"] == 0:
        verdict = "TOOK NOTHING under the proposed value — see before/after n"
    else:
        verdict = ("IMPROVES" if delta > 0 else
                  "DETERIORATES" if delta < 0 else "NEUTRAL")
        verdict += f" realised R by {delta:+.2f}R"

    return {
        "method": "walk_forward_config_swap",
        "target_key": key, "current_value": current, "proposed_value": proposed,
        "days_requested": days, "days_used": before["sessions"],
        "before": before, "after": after,
        "delta_r": round(delta, 4),
        "underpowered": underpowered,
        "verdict": verdict,
        "note": "Walk-forward over resolved intraday_setups, same machinery "
                "as tools.allocator_replay.replay()'s NEW column. A CEILING/"
                "ESTIMATE like every replay tool in this project — see "
                "REPLAY_MIN_SESSIONS and this module's own docstring for the "
                ">=20 outcomes / >=10 sessions standard.",
    }


def run(days: int = 30, proposal_id: int | None = None, dry_run: bool = False,
        sb=None) -> int:
    sb = sb or get_supabase()

    q = sb.table("brain_proposals").select(
        "id,proposal_type,target_key,current_value,proposed_value,status")
    q = q.eq("id", proposal_id) if proposal_id is not None else q.eq("status", "PENDING")
    proposals = q.execute().data or []
    if not proposals:
        logger.warning("  no matching proposal(s) — nothing to backtest")
        return 0

    floor_n = cfg_int("priors_min_sample_intraday", 30)
    hurdle_pct = cfg_float("alloc_hurdle_percentile", 0.75)
    floor_edge = cfg_float("alloc_edge_absolute_floor", 0.0)
    cap = cfg_int("intraday_max_new_per_day", 4)

    rows = None  # fetched lazily, only if a testable proposal is found
    tested, skipped = 0, 0

    for row in proposals:
        kind, reason = classify_proposal(row)
        logger.info(f"  #{row['id']} [{row.get('proposal_type')}] "
                    f"{row.get('target_key')}: {kind} — {reason}")

        if kind == NOT_TESTABLE:
            result = {"method": "none", "testable": False, "reason": reason}
            skipped += 1
        else:
            if rows is None:
                since = (today_ist() - timedelta(days=days + 120)).isoformat()
                rows = _fetch_setups(sb, since)
            if not rows:
                result = {"method": "none", "testable": False,
                          "reason": "no resolved intraday_setups rows to replay"}
                skipped += 1
            else:
                result = backtest_config_proposal(
                    row, rows, days, hurdle_pct, floor_edge, floor_n, cap)
                result["testable"] = True
                logger.info(f"      {result['verdict']}"
                            + ("  [UNDERPOWERED]" if result["underpowered"] else ""))
                tested += 1

        if not dry_run:
            try:
                sb.table("brain_proposals").update(
                    {"backtest_result": json.dumps(result, default=str)}
                ).eq("id", row["id"]).execute()
            except Exception as e:
                logger.warning(f"      write failed for #{row['id']}: {e}")

    logger.info("")
    logger.info(f"  {tested} backtested, {skipped} not mechanically testable"
                + (" (dry run — nothing written)" if dry_run else ""))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Backtest brain_proposals and write backtest_result")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--id", type=int, default=None, help="one specific proposal id")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, write nothing")
    a = ap.parse_args()
    sys.exit(run(days=a.days, proposal_id=a.id, dry_run=a.dry_run))
