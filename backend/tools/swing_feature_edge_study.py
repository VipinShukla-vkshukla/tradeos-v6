"""
Which SPECIFIC setups within a swing engine family work — not just whether
the family does. Stage E2 of Track E (docs/TRADEOS_ROADMAP.md).

    python -m tools.swing_feature_edge_study                 default, live proposals
    python -m tools.swing_feature_edge_study --dry-run
    python -m tools.swing_feature_edge_study --since 2026-07-01

WHY THIS IS A DIFFERENT QUESTION FROM EVERY TERCILE STUDY RUN SO FAR
------------------------------------------------------------------------
`final_score` and `implied_rr` were each tercile-tested ONE COLUMN AT A
TIME against forward R and found flat (`entry_ranking.py`'s own
comments). That is real evidence about those two features in isolation —
it says nothing about combinations, and it says nothing broken down by
engine family. HAL (CTL, 21-Aug-2026) chased 1.2%+ above its own zone and
lost; this tool asks, book-wide and per family, whether chase distance —
or any of the dozen other fields `signal_output_daily` already carries —
actually separates winners from losers, the same discretionary-trader
question `tools/feature_edge_study.py` asks on the intraday side.

DOES NOT IMPORT FROM `intraday/` OR `tools/feature_edge_study.py` —
DELIBERATE, PER THE OPERATOR'S EXPLICIT INSTRUCTION
------------------------------------------------------------------------
The intraday tool's tercile/significance machinery is generic statistics
and could technically be imported — it was not, on purpose. Track E's own
non-negotiables (docs/TRADEOS_ROADMAP.md) commit to zero dependency on
any intraday-owned file, and `tools/feature_edge_study.py` is intraday's,
not shared infrastructure the way `allocation/scoring.py` is. The METHOD
(extreme-tercile split for numeric features, bucket-vs-rest for
categorical ones, the same significance bar shape) is reused because it
is sound and already proven out live; the CODE is independent, swing-
owned, and swing-tested. The one import this module does take —
`allocation.scoring.swing_family` — is the same read-only dependency
F-46 already established as safe: shared allocator infrastructure both
tracks depend on, never edited by either.

WHAT A FINDING IS AND IS NOT
------------------------------------------------------------------------
A split here is a HYPOTHESIS, never a rule change. It is written to
`brain_proposals` with `target_key` prefixed `SWING/` (never colliding
with an intraday engine name) and `source="swing_feature_edge_study"` —
same table intraday's own tools write to, since it is a shared "propose,
never auto-apply" queue, not intraday-owned. Nothing here can change what
any gate admits; it can only ask an operator to look at a number. Stage
E6 is where a VALIDATED finding (out-of-sample re-checked, the F-50
pattern) is allowed to influence `entry_ranking.score_plan()` — this
stage stops at raising the hypothesis.

ONE KNOWN, BOUNDED INTERACTION WITH THE SHARED QUEUE — named, not hidden.
`tools/feature_edge_study.py::validate_pending()` reads every
`status=PENDING, proposal_type=FEATURE_FILTER` row regardless of its
`target_key` prefix, to re-validate it against `intraday_setups`. A row
this tool writes (`SWING/...`) will not match any real intraday engine
name, so it will land in that function's "not enough fresh data yet"
branch and stay PENDING, harmless — never silently misread as validated
or rejected. Confirmed by reading that function's own filter, not
assumed; not fixed, because fixing it would mean editing an intraday
file, which this track does not do. Stage E6 builds this tool's own,
independent out-of-sample validator rather than relying on the intraday
one ever reaching a SWING/ row correctly.

TERCILES, NOT A MEDIAN SPLIT — same reasoning as the intraday tool: the
extreme thirds sharpen a real effect and resist a noisy boundary cutoff
landing on the split point the way a single median split would not.

NO FLOOR DATE, UNLIKE THE INTRADAY VERSION — considered, not omitted.
`priors_intraday_since` protects intraday's prior from a stop-geometry
change (F-33) that altered how R was computed for every future detection.
No equivalent contamination event exists for swing's ENTRY-side features
— F-43/F-46 changed EXIT management (partial/giveback/stall timing), not
which stocks get selected or how a plan's own zone/stop/target resolve
against price. `--since` exists for a manual narrower look, not because
a floor is owed by default.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_int, get_supabase, today_ist, fetch_all

# Below this many resolved (TARGET+STOP) rows for one engine family, no
# split is attempted. Measured live, 24-Aug-2026: CONTINUATION n=292,
# MOM n=78 clear this comfortably; RVS n=10, SBS n=8, RSB n=13, IAD n=3 do
# not and are silently skipped, not guessed at — the exact "cold start
# must be permissive, never invent a rule from a thin sample" principle
# this project's own priors already follow.
MIN_ENGINE_SAMPLE = 40

# Per side of a split. A gap between two 8-row segments is an anecdote.
MIN_SEGMENT = 15

# Win-rate gap, in percentage points, between the extreme thirds (or a
# category vs the rest) before it is worth an operator's time. Swing's
# measured base win rates run 30-90% by peak-MFE bucket (F-43's own
# stall-rule table) — a wider spread than intraday's typical 35-55%, so
# the bar is set a touch higher to keep the same "large, visibly-not-
# noise gap" standard rather than reusing intraday's 20pp unexamined.
WIN_RATE_LIFT_PP = 25.0

# Mean realised-return-pct gap (percentage points) as the alternative
# trigger — catches a split where losers lose bigger/smaller rather than
# more/less often, even when the win-rate gap alone does not clear the bar.
MEAN_PCT_GAP = 1.5

# Every one of these already sits on signal_output_daily as a plain
# column — no meta-JSON parsing needed, unlike the intraday shape.
NUMERIC_FEATURES = (
    "rsi_daily", "rsi_weekly", "adx", "vol_ratio", "delivery_pct",
    "atr_pct", "institutional_score", "risk_score", "holding_score",
    "momentum_score", "ma_alignment_score", "sector_rank_at_entry",
)
CATEGORICAL_FEATURES = (
    "sector", "entry_timing_type", "momentum_state", "velocity_state",
    "bb_context", "vwap_alignment", "weekly_structure", "macd_direction",
)


# ── pure helpers — no I/O, fully unit-testable ───────────────────────────────

def engine_key(r: dict) -> str:
    """
    The engine FAMILY a resolved plan belongs to — CONTINUATION pools
    CTL/SEC/TPO/SBS/RSB/IAD/VBD (the blueprint's own deliberate grouping,
    too fine individually for a stable split); MOM and RVS stay isolated,
    exactly as `swing_family()` already treats them for the allocator's
    own priors and F-46's stall-clock calibration. Read-only import —
    this module never edits `allocation/scoring.py`.
    """
    from allocation.scoring import swing_family
    return swing_family(r.get("strategy"))


def _numeric_value(r: dict, feature: str) -> float | None:
    v = r.get(feature)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v


def _win_rate(rows: list[dict]) -> tuple[float | None, int, int, int]:
    """(win_rate, target_n, stop_n, other_n). None if TARGET+STOP == 0 —
    a segment that is only OTHER (e.g. every row already filtered to
    TARGET/STOP upstream, so this is mostly a defensive floor) says
    nothing about accuracy."""
    tgt = sum(1 for r in rows if r.get("outcome_category") == "TARGET")
    stp = sum(1 for r in rows if r.get("outcome_category") == "STOP")
    other = len(rows) - tgt - stp
    denom = tgt + stp
    return (tgt / denom if denom else None), tgt, stp, other


def _mean_pct(rows: list[dict]) -> float | None:
    vals = [float(r["outcome_return_pct"]) for r in rows
            if r.get("outcome_return_pct") is not None]
    return (sum(vals) / len(vals)) if vals else None


def _significant(lo_rows: list[dict], hi_rows: list[dict],
                 min_segment: int = MIN_SEGMENT) -> dict | None:
    """Shared verdict logic for both split kinds. `lo`/`hi` are just
    "segment A" / "segment B" — the caller decides what they mean (bottom
    vs top tercile, or one category vs the rest)."""
    if len(lo_rows) < min_segment or len(hi_rows) < min_segment:
        return None
    lo_wr, lo_t, lo_s, lo_o = _win_rate(lo_rows)
    hi_wr, hi_t, hi_s, hi_o = _win_rate(hi_rows)
    lo_pct, hi_pct = _mean_pct(lo_rows), _mean_pct(hi_rows)

    wr_gap = (None if lo_wr is None or hi_wr is None
             else abs(hi_wr - lo_wr) * 100.0)
    pct_gap = (None if lo_pct is None or hi_pct is None
              else abs(hi_pct - lo_pct))

    fires = ((wr_gap is not None and wr_gap >= WIN_RATE_LIFT_PP) or
            (pct_gap is not None and pct_gap >= MEAN_PCT_GAP))
    if not fires:
        return None
    return {
        "lo_n": len(lo_rows), "hi_n": len(hi_rows),
        "lo_win_rate": lo_wr, "hi_win_rate": hi_wr,
        "lo_mean_pct": lo_pct, "hi_mean_pct": hi_pct,
        "lo_target": lo_t, "lo_stop": lo_s, "lo_other": lo_o,
        "hi_target": hi_t, "hi_stop": hi_s, "hi_other": hi_o,
        "win_rate_gap_pp": wr_gap, "mean_pct_gap": pct_gap,
    }


def numeric_split(rows: list[dict], feature: str,
                  min_segment: int = MIN_SEGMENT) -> dict | None:
    """Bottom third vs top third of `feature`'s value, dropping the
    middle third. None when there is nothing to report."""
    valued = [(r, _numeric_value(r, feature)) for r in rows]
    valued = [(r, v) for r, v in valued if v is not None]
    if len(valued) < min_segment * 2:
        return None
    valued.sort(key=lambda rv: rv[1])
    n = len(valued)
    third = n // 3
    if third < min_segment:
        return None
    lo_rows = [r for r, _ in valued[:third]]
    hi_rows = [r for r, _ in valued[n - third:]]
    lo_bound, hi_bound = valued[third - 1][1], valued[n - third][1]
    found = _significant(lo_rows, hi_rows, min_segment)
    if not found:
        return None
    found.update({"feature": feature, "kind": "numeric",
                 "lo_label": f"<= {lo_bound:.3g}", "hi_label": f">= {hi_bound:.3g}"})
    return found


def categorical_splits(rows: list[dict], feature: str,
                       min_segment: int = MIN_SEGMENT) -> list[dict]:
    """Each category with enough samples, compared against every OTHER
    row (not against the other categories individually). Returns a list —
    more than one category can legitimately fire in one pass."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        v = r.get(feature)
        if not v:
            continue
        by_cat[str(v)].append(r)

    out = []
    for cat, cat_rows in by_cat.items():
        rest = [r for r in rows if r not in cat_rows]
        found = _significant(rest, cat_rows, min_segment)
        if not found:
            continue
        found.update({"feature": feature, "kind": "categorical", "category": cat,
                     "lo_label": f"NOT {cat}", "hi_label": cat})
        out.append(found)
    return out


def target_key_for(engine: str, found: dict) -> str:
    """
    The brain_proposals dedup key, `SWING/`-prefixed so this can never
    collide with an intraday engine's own key in the shared table.
    Category included, not just feature — mirrors F-50's own fix
    (target_key_for in the intraday tool), a categorical feature can
    legitimately fire for more than one category in a single pass.
    """
    key = f"SWING/{engine}/{found['feature']}"
    if found.get("category"):
        key += f"/{found['category']}"
    return key


def is_favourable(found: dict) -> bool | None:
    """Does the "hi" side (the category itself, for a categorical
    finding) predict a BETTER outcome than "lo" (the rest)? None when
    neither win-rate nor mean-pct can decide — a caller must treat that
    as "do not know", never a default direction."""
    hi_wr, lo_wr = found.get("hi_win_rate"), found.get("lo_win_rate")
    if hi_wr is not None and lo_wr is not None and hi_wr != lo_wr:
        return hi_wr > lo_wr
    hi_pct, lo_pct = found.get("hi_mean_pct"), found.get("lo_mean_pct")
    if hi_pct is not None and lo_pct is not None and hi_pct != lo_pct:
        return hi_pct > lo_pct
    return None


def _evidence(engine: str, found: dict) -> str:
    lo_wr = f"{found['lo_win_rate']:.0%}" if found['lo_win_rate'] is not None else "n/a"
    hi_wr = f"{found['hi_win_rate']:.0%}" if found['hi_win_rate'] is not None else "n/a"
    lo_pct = f"{found['lo_mean_pct']:+.2f}%" if found['lo_mean_pct'] is not None else "n/a"
    hi_pct = f"{found['hi_mean_pct']:+.2f}%" if found['hi_mean_pct'] is not None else "n/a"
    return (f"SWING/{engine}/{found['feature']}: {found['lo_label']} -> win {lo_wr} "
           f"({found['lo_target']}T/{found['lo_stop']}S/{found['lo_other']}other, "
           f"n={found['lo_n']}, mean {lo_pct}) vs {found['hi_label']} -> win {hi_wr} "
           f"({found['hi_target']}T/{found['hi_stop']}S/{found['hi_other']}other, "
           f"n={found['hi_n']}, mean {hi_pct}). Extreme-tercile / bucket-vs-rest "
           f"comparison, entered-and-resolved (TARGET/STOP) rows only.")


# ── I/O boundary — thin, deliberately not unit-tested ────────────────────────

def _rows(sb, since: str | None) -> list[dict]:
    cols = ("strategy,outcome_category,outcome_return_pct,symbol,date,"
           + ",".join(NUMERIC_FEATURES) + "," + ",".join(CATEGORICAL_FEATURES))

    def build():
        q = (sb.table("signal_output_daily").select(cols)
              .eq("outcome_entered", True)
              .in_("outcome_category", ["TARGET", "STOP"]))
        return q.gte("date", since) if since else q

    # signal_output_daily has no id column — (symbol, date) is its unique
    # key, the same order_by swing_priors()/pace_calibration.py already
    # use for the identical reason.
    return fetch_all(build, order_by="symbol,date")


def _propose(sb, run_id: str, engine: str, found: dict,
            dry_run: bool = False) -> bool:
    target_key = target_key_for(engine, found)
    evidence = _evidence(engine, found)
    gap = found.get("win_rate_gap_pp") or 0.0
    confidence = round(min(0.4 + gap / 100.0, 0.85), 2)
    fav = is_favourable(found)
    direction = "favourable" if fav is True else "unfavourable" if fav is False else "unclear"
    if dry_run:
        logger.info(f"  [DRY RUN] would propose {target_key} ({direction}, "
                   f"confidence {confidence})")
        logger.info(f"    {evidence}")
        return True
    row = {"analysis_run_id": run_id, "proposal_type": "FEATURE_FILTER",
           "target_key": target_key, "current_value": direction,
           "proposed_value": f"review a floor/band on {found['feature']} for SWING/{engine}",
           "evidence": evidence, "rationale": evidence, "confidence": confidence,
           "status": "PENDING", "source": "swing_feature_edge_study", "priority": 3}
    try:
        existing = (sb.table("brain_proposals").select("id")
                     .eq("proposal_type", "FEATURE_FILTER").eq("target_key", target_key)
                     .eq("status", "PENDING").execute().data or [])
        if existing:
            sb.table("brain_proposals").update(row).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("brain_proposals").insert(row).execute()
        return True
    except Exception as e:
        logger.warning(f"  could not record finding ({target_key}): {e}")
        return False


def study(sb, since: str | None, run_id: str,
         min_engine_sample: int = MIN_ENGINE_SAMPLE,
         dry_run: bool = False) -> dict[str, list[dict]]:
    """Orchestration. Returns {engine_family: [finding, ...]} for the
    caller (CLI or a future E6 tool) to inspect, independent of whether
    anything was actually written."""
    rows = _rows(sb, since)
    by_engine: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_engine[engine_key(r)].append(r)

    out: dict[str, list[dict]] = {}
    for engine, erows in sorted(by_engine.items()):
        if len(erows) < min_engine_sample:
            logger.info(f"  {engine}: n={len(erows)} — below the {min_engine_sample} "
                       f"floor, skipped")
            continue
        found_list = []
        for feat in NUMERIC_FEATURES:
            f = numeric_split(erows, feat)
            if f:
                found_list.append(f)
        for feat in CATEGORICAL_FEATURES:
            found_list.extend(categorical_splits(erows, feat))
        logger.info(f"  {engine}: n={len(erows)}, {len(found_list)} finding(s)")
        for f in found_list:
            _propose(sb, run_id, engine, f, dry_run=dry_run)
        out[engine] = found_list
    return out


def main(since: str | None = None, min_engine_sample: int = MIN_ENGINE_SAMPLE,
        dry_run: bool = False) -> dict[str, list[dict]]:
    sb = get_supabase()
    run_id = f"swing_feature_edge_{today_ist().isoformat()}"
    logger.info(f"  swing feature-edge study — run {run_id}"
               + (f", since {since}" if since else ", full history"))
    return study(sb, since, run_id, min_engine_sample=min_engine_sample, dry_run=dry_run)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--min-engine-sample", type=int, default=MIN_ENGINE_SAMPLE)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    main(since=a.since, min_engine_sample=a.min_engine_sample, dry_run=a.dry_run)
