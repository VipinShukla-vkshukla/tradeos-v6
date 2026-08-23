"""
Which SPECIFIC setups within an engine work — not just whether the engine does.

    python -m tools.feature_edge_study                 default floor, live proposals
    python -m tools.feature_edge_study --since 2026-08-18 --dry-run

WHY THIS IS A DIFFERENT QUESTION FROM EVERYTHING ELSE THAT LEARNS HERE
------------------------------------------------------------------------
`intraday_priors()` and the allocator's bar answer "is this ENGINE, on
average, worth taking a trade from" — one number per engine, blind to
whether the SPECIFIC candidate in front of it looks like the engine's best
work or its worst. `weekly_review.py` scores engines and gates.
`discover_engines.py` looks for edges no engine has coded at all. None of
them asks the question a discretionary trader asks constantly: *of the
trades I actually took, what did the winners have in common that the
losers didn't?*

Every resolved TAKEN row already carries the raw material for that —
`volume_ratio`, `atr_pct_daily`, `confidence`, `sector`, `regime_at_
detection`, hour of detection — sitting unused once the outcome is scored.
This is the first tool that mines it.

WHAT A FINDING IS AND IS NOT — same discipline as discover_engines.py
------------------------------------------------------------------------
A split here is a HYPOTHESIS, never a rule change. It is written to
`brain_proposals` exactly like an engine retirement or a gate loosening —
same table, same review queue, same "propose, never auto-apply" contract
this whole project runs on. Nothing this module does can change what the
allocator admits; it can only ask an operator to look at a number.

THE BAR FOR REPORTING IS DELIBERATELY HIGH, for the same reason
discover_engines.py's is: given enough features and engines, noise
produces an interesting-looking split constantly. MIN_SEGMENT and
WIN_RATE_LIFT_PP exist so a finding has to be surprising and adequately
sampled, not merely present. See `_numeric_split`/`_categorical_split`.

WHY TERCILES, NOT A MEDIAN SPLIT
------------------------------------------------------------------------
scoring.py's own 19-Aug confidence-band measurement already terciled each
engine's population and reported the low/mid/high split — this reuses that
exact convention rather than inventing a second one. Comparing the extreme
thirds (dropping the middle) sharpens a real effect and is more resistant
to a borderline cutoff landing on a noisy boundary than a single median
split would be.

THE FLOOR DATE — reuses `priors_intraday_since`, DELIBERATELY
------------------------------------------------------------------------
The same contamination `priors_intraday_since` protects the per-engine
prior from (F-33's 18-Aug stop-geometry change, the 20-Aug sub_engine/
meta-encoding fixes) applies here identically — a feature correlation
computed across two different stop geometries is not measuring one thing.
Default behaviour honours that floor exactly, which means a run on the
night this floor is armed will legitimately find little yet; `--since`
exists to run a wider, manual look (e.g. from 18-Aug, post F-33 only) when
that is the question being asked, not to bypass the floor's own reasoning.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg, cfg_int, get_supabase, today_ist, fetch_all

# Below this many resolved rows for one (engine/condition), no split is even
# attempted — matches discover_engines.py's MIN_OCCURRENCES reasoning: an
# engine with 8 resolved trades cannot be terciled into three groups anyone
# should trust.
MIN_ENGINE_SAMPLE = 40

# Per side of a split (per tercile, per category-vs-rest). Below this a gap
# is an anecdote. Chosen higher than discover_engines.py's MIN_OCCURRENCES=6
# because this compares two SUBSETS of one already-thin population rather
# than one bucket against a large background.
MIN_SEGMENT = 15

# A win-rate gap this large, in percentage points, between the extreme
# thirds (or a category vs the rest) before it is worth an operator's time.
# 20pp on intraday data (typically 35-55% base win rates) is a large,
# visibly-not-noise gap, not a hair's-width threshold that would fire on
# every run.
WIN_RATE_LIFT_PP = 20.0

# Alternative trigger: mean outcome_pct (already net, signed in the trade's
# favour) differs by at least this many percentage points between segments,
# even if the win-rate gap alone does not clear WIN_RATE_LIFT_PP — catches
# a split where losers lose bigger/smaller rather than more/less often.
MEAN_PCT_GAP = 0.15

NUMERIC_FEATURES = ("volume_ratio", "atr_pct_daily", "confidence")
CATEGORICAL_FEATURES = ("sector", "regime_at_detection", "_hour_bucket")


# ── pure helpers — no I/O, fully unit-testable ───────────────────────────────

def _meta(r: dict) -> dict:
    """Defensive re-parse — same tolerance _engine_of() already applies,
    because meta has shipped as a JSON string before (F-40) and a reader
    that assumes a dict is exactly the class of bug that fix closed."""
    m = r.get("meta") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except (ValueError, TypeError):
            return {}
    return m if isinstance(m, dict) else {}


def engine_key(r: dict) -> str:
    """The condition, not just the family — SDN's three conditions are
    supposed to be studied separately once sub_engine correctly separates
    them (F-39/F-41). Reuses scoring._engine_of rather than a second
    definition of the same lookup."""
    from allocation.scoring import _engine_of
    return _engine_of(r)


def _hour_bucket(ts: str | None) -> str | None:
    """OPEN (09:15-10:00, the volatile opening hour), MID (10:00-13:00),
    LATE (13:00-15:15) — coarse enough to have real sample per bucket,
    fine enough to separate the opening-range regime from the drift a lot
    of mean-reversion engines are built for."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(IST)
    except (ValueError, TypeError):
        return None
    hm = dt.hour * 60 + dt.minute
    if hm < 10 * 60:
        return "OPEN"
    if hm < 13 * 60:
        return "MID"
    return "LATE"


def _numeric_value(r: dict, feature: str) -> float | None:
    if feature == "confidence":
        v = r.get("confidence")
    else:
        v = _meta(r).get(feature)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v


def _win_rate(rows: list[dict]) -> tuple[float | None, int, int, int]:
    """(win_rate, target_n, stop_n, other_n). None if TARGET+STOP == 0 —
    a segment made only of TIMEOUT/UNKNOWN says nothing about accuracy."""
    tgt = sum(1 for r in rows if r.get("outcome") == "TARGET")
    stp = sum(1 for r in rows if r.get("outcome") == "STOP")
    other = len(rows) - tgt - stp
    denom = tgt + stp
    return (tgt / denom if denom else None), tgt, stp, other


def _mean_pct(rows: list[dict]) -> float | None:
    vals = [float(r["outcome_pct"]) for r in rows if r.get("outcome_pct") is not None]
    return (sum(vals) / len(vals)) if vals else None


def _significant(lo_rows: list[dict], hi_rows: list[dict],
                  min_segment: int = MIN_SEGMENT) -> dict | None:
    """Shared verdict logic for both split kinds. Returns a finding dict or
    None. `lo`/`hi` are just "segment A" / "segment B" — the caller decides
    what they mean (bottom vs top tercile, or one category vs the rest)."""
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
    """
    Bottom third vs top third of `feature`'s value within `rows`, dropping
    the middle third — see the module docstring for why terciles.

    Returns None when there is nothing to report: too few rows carry the
    feature, or neither segment clears the significance bar.
    """
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
    """
    Each category with enough samples, compared against every OTHER row in
    `rows` (not against the other categories individually) — mirrors
    discover_engines.py's bucket-vs-background shape. Returns a list
    because, unlike a numeric split, more than one category can legitimately
    fire in the same pass (e.g. two sectors, each standing out for a
    different reason).
    """
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if feature == "_hour_bucket":
            v = _hour_bucket(r.get("ts"))
        else:
            v = _meta(r).get(feature) or r.get(feature)
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
    The brain_proposals dedup key. CATEGORY INCLUDED, NOT JUST FEATURE — a
    categorical feature can legitimately fire for several categories in one
    pass (SDN disliking both "i.t" and "metals & mining" are two different
    findings). Without the category, every subsequent category for the same
    (engine, feature) collided on one key, and _propose()'s dedup lookup
    treated the second finding as "already proposed" and overwrote the
    first — silently discarding every finding but the last one processed.
    Numeric splits carry no category, so their key is unchanged.
    """
    key = f"{engine}/{found['feature']}"
    if found.get("category"):
        key += f"/{found['category']}"
    return key


def is_favourable(found: dict) -> bool | None:
    """
    Does the "hi" side of this split (the category itself, for a
    categorical finding) predict a BETTER outcome than the "lo" side (the
    rest)? None when neither win-rate nor mean-pct can decide it (both
    absent or exactly tied) — a caller must treat that as "do not know",
    never as a default direction.

    Win rate decides first; mean_pct is the tiebreak for the rare case
    win rate ties or is unavailable on one side (a segment made only of
    TIMEOUT/UNKNOWN — see _win_rate's own docstring). Kept as ONE pure
    function, read by both `_propose()` (to store the direction) and this
    module's own tests, so "favourable" can never mean something subtly
    different in the two places that ask.
    """
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
    return (f"{engine}/{found['feature']}: {found['lo_label']} -> win {lo_wr} "
           f"({found['lo_target']}T/{found['lo_stop']}S/{found['lo_other']}other, "
           f"n={found['lo_n']}, mean {lo_pct}) vs {found['hi_label']} -> win {hi_wr} "
           f"({found['hi_target']}T/{found['hi_stop']}S/{found['hi_other']}other, "
           f"n={found['hi_n']}, mean {hi_pct}). Extreme-tercile / bucket-vs-rest "
           f"comparison, TAKEN-and-resolved rows only.")


# ── I/O boundary — thin, deliberately not unit-tested ────────────────────────

def _floor_since(explicit: str | None) -> str:
    if explicit:
        return explicit
    lookback = cfg_int("priors_intraday_lookback_days", 90)
    since = (today_ist() - timedelta(days=max(lookback, 1))).isoformat()
    floor_date = (cfg("priors_intraday_since", "") or "").strip()
    return max(since, floor_date) if floor_date else since


def _rows(sb, since: str) -> list[dict]:
    return fetch_all(lambda: sb.table("intraday_setups")
                     .select("id,strategy,meta,confidence,regime_at_detection,"
                             "ts,outcome,outcome_pct,trade_date")
                     .eq("cost_verdict", "TAKEN")
                     .not_.is_("outcome", "null")
                     .gte("trade_date", since))


def _propose(sb, run_id: str, engine: str, found: dict,
            dry_run: bool = False) -> bool:
    target_key = target_key_for(engine, found)
    evidence = _evidence(engine, found)
    gap = found.get("win_rate_gap_pp") or 0.0
    # Confidence scales with the win-rate gap, capped — a 20pp finding is
    # worth a look; a 60pp one on the same sample sizes is not 3x more
    # certain, so this saturates rather than growing unbounded.
    confidence = round(min(0.4 + gap / 100.0, 0.85), 2)
    if dry_run:
        logger.info(f"  [DRY RUN] would propose {target_key} (confidence {confidence})")
        logger.info(f"    {evidence}")
        return True
    # DIRECTION, STORED STRUCTURED — not left to be parsed back out of the
    # prose `evidence` string later. `current_value` is reused as this
    # tag deliberately (it previously held a static, uninformative "no
    # feature-level filter" for every row): "favourable"/"unfavourable"/
    # "unclear" is a controlled, three-value vocabulary `allocation.
    # allocator.refresh_priority_criteria()` can read with total
    # confidence, where regexing a human-readable sentence could not.
    fav = is_favourable(found)
    direction = "favourable" if fav is True else "unfavourable" if fav is False else "unclear"
    row = {"analysis_run_id": run_id, "proposal_type": "FEATURE_FILTER",
           "target_key": target_key, "current_value": direction,
           "proposed_value": f"review a floor/band on {found['feature']} for {engine}",
           "evidence": evidence, "rationale": evidence, "confidence": confidence,
           "status": "PENDING", "source": "feature_edge_study", "priority": 3}
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


def _validation_outcome(current_value: str | None,
                        fresh_favourable: bool | None) -> bool | None:
    """
    Pure. Does a fresh out-of-sample check CONFIRM the original finding's
    direction? True/False, or None when the question cannot be answered
    at all — which must never be silently read as False.

    THE 23-Aug-2026 BUG THIS EXISTS TO NOT REPEAT. The inline version of
    this compared `current_value == "favourable"` directly — so ANY other
    value, including "unclear" (a genuine original no-opinion) and the
    placeholder text every row written before F-50's direction field
    existed still carried ("no feature-level filter"), read as an
    explicit UNFAVOURABLE claim nobody had actually made. Confirmed live:
    ORB/_hour_bucket/MID's real Aug-20 finding said MID was the GOOD side;
    the fresh check agreed; the bug still emitted REJECTED, because a row
    predating this field was read as if it had confidently claimed the
    opposite. The same "no opinion must not read as measured bad" mistake
    this project's own priors have made before, one layer up, here in the
    validator itself.

    `current_value` must be a REAL tag — "favourable" or "unfavourable",
    nothing else — or this returns None (cannot validate, caller must
    skip, never guess a direction).
    """
    if current_value not in ("favourable", "unfavourable"):
        return None
    if fresh_favourable is None:
        return None
    return fresh_favourable == (current_value == "favourable")


def validate_pending(sb, min_segment: int = MIN_SEGMENT,
                     dry_run: bool = False) -> tuple[int, int, int]:
    """
    OUT-OF-SAMPLE VALIDATION — 22-Aug-2026, F-50.

    Every PENDING, CATEGORICAL FEATURE_FILTER proposal (3-part target_key
    — `engine/feature/category`; numeric 2-part findings are not handled
    here, same boundary as `refresh_priority_criteria`) gets re-checked
    against rows that closed AFTER the proposal was first written — data
    the original finding could not have seen. This is the difference
    between "this pattern fit the data it was found in" (guaranteed,
    trivially) and "this pattern predicts data it had not seen yet" (the
    only claim that actually matters before anything acts on it).

    THREE OUTCOMES, NOT TWO. A finding either (a) still shows the SAME
    direction on fresh data at the same significance bar — VALIDATED,
    eligible for `allocation.allocator.refresh_priority_criteria()`'s
    cache; (b) shows the OPPOSITE direction, or falls below the bar —
    REJECTED, and stays in the table as a record of a hypothesis that did
    not hold, never deleted; or (c) there simply is not enough fresh data
    yet to say either way — stays PENDING, unchanged. Collapsing (c) into
    either of the other two is exactly the "measured bad" vs "no opinion"
    confusion this project's own landmines warn about at the prior level,
    one layer up.

    Returns (validated, rejected, skipped_insufficient_data).
    """
    from datetime import date
    rows = fetch_all(lambda: sb.table("brain_proposals").select("*")
                     .eq("status", "PENDING").eq("proposal_type", "FEATURE_FILTER")
                     .order("id"))
    validated = rejected = skipped = 0
    for r in rows:
        parts = str(r.get("target_key") or "").split("/")
        if len(parts) != 3:
            continue  # numeric finding — not this pass's job
        engine, feature, category = parts
        created = r.get("created_at")
        if not created:
            skipped += 1
            continue
        try:
            # Strictly AFTER the day the proposal was created — same-day
            # overlap would let the "fresh" check re-graze data the
            # original finding already used, which is not out-of-sample
            # at all, just the same sample re-read.
            fresh_since = (date.fromisoformat(str(created)[:10]) + timedelta(days=1)).isoformat()
        except ValueError:
            skipped += 1
            continue

        fresh_rows = [x for x in _rows(sb, fresh_since) if engine_key(x) == engine]
        findings = categorical_splits(fresh_rows, feature, min_segment=min_segment)
        match = next((f for f in findings if f.get("category") == category), None)
        if match is None:
            skipped += 1
            continue

        # See _validation_outcome's own docstring for the 23-Aug bug this
        # replaced — reading a pre-F-50 row's placeholder current_value
        # as an explicit "unfavourable" claim nobody had made.
        outcome = _validation_outcome(r.get("current_value"), is_favourable(match))
        if outcome is None:
            skipped += 1
            continue
        holds = outcome
        new_status = "VALIDATED" if holds else "REJECTED"
        logger.info(f"  {'✓ VALIDATED' if holds else '✗ REJECTED'} "
                   f"{r['target_key']}: {_evidence(engine, match)}")
        if dry_run:
            if holds:
                validated += 1
            else:
                rejected += 1
            continue
        try:
            sb.table("brain_proposals").update({
                "status": new_status, "reviewed_at": datetime.now().isoformat(),
                "backtest_result": _evidence(engine, match),
            }).eq("id", r["id"]).execute()
            if holds:
                validated += 1
            else:
                rejected += 1
        except Exception as e:
            logger.warning(f"  could not update validation status for {r['target_key']}: {e}")
            skipped += 1
    return validated, rejected, skipped


def study(sb, since: str, run_id: str, min_engine_sample: int = MIN_ENGINE_SAMPLE,
         dry_run: bool = False) -> int:
    rows = _rows(sb, since)
    if not rows:
        logger.info(f"  feature_edge_study: no TAKEN-and-resolved rows since {since}")
        return 0

    by_engine: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_engine[engine_key(r)].append(r)

    logger.info(f"  feature_edge_study: {len(rows)} row(s) since {since}, "
               f"{len(by_engine)} engine/condition group(s)")

    n_proposed = 0
    for engine, grp in sorted(by_engine.items(), key=lambda kv: -len(kv[1])):
        if len(grp) < min_engine_sample:
            logger.debug(f"    {engine}: {len(grp)} row(s), below the "
                        f"{min_engine_sample}-sample floor — skipped")
            continue
        findings = []
        for feat in NUMERIC_FEATURES:
            f = numeric_split(grp, feat)
            if f:
                findings.append(f)
        for feat in CATEGORICAL_FEATURES:
            findings.extend(categorical_splits(grp, feat))

        if not findings:
            logger.info(f"    {engine}: {len(grp)} row(s), nothing separates "
                       f"winners from losers past the reporting bar")
            continue
        for f in findings:
            logger.warning(f"    {engine}: {_evidence(engine, f)}")
            if _propose(sb, run_id, engine, f, dry_run=dry_run):
                n_proposed += 1
    return n_proposed


def main(since: str | None = None, min_engine_sample: int = MIN_ENGINE_SAMPLE,
        dry_run: bool = False) -> int:
    from datetime import datetime as _dt
    sb = get_supabase()
    floor = _floor_since(since)
    run_id = f"feature_study_{_dt.now():%Y%m%d_%H%M%S}"
    logger.info("═" * 72)
    logger.info("TradeOS — feature-level edge study (proposes FEATURE_FILTER only)")
    logger.info("═" * 72)

    # VALIDATE FIRST, THEN LOOK FOR NEW SPLITS — so a finding from a prior
    # run gets its chance to earn (or lose) VALIDATED status on fresh data
    # before this same run's new findings join the PENDING queue behind it.
    v, rj, sk = validate_pending(sb, dry_run=dry_run)
    if v or rj:
        logger.info(f"  out-of-sample check: {v} validated, {rj} rejected, "
                   f"{sk} not enough fresh data yet")

    n = study(sb, floor, run_id, min_engine_sample, dry_run=dry_run)
    logger.info("")
    if n:
        logger.warning(f"  {n} finding(s) raised — read them with `tradeos learn show`")
        logger.info("  Each is a HYPOTHESIS over a real but limited sample. It proposes "
                    "reviewing a floor/band, never sets one.")
    else:
        logger.success("  no findings — nothing in the data separates winners from "
                       "losers past the reporting bar")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Find which setup FEATURES (not just which engine) predict outcome")
    ap.add_argument("--since", default=None,
                    help="override the floor date (default: priors_intraday_since, "
                         "same floor the allocator itself trusts)")
    ap.add_argument("--min-sample", type=int, default=MIN_ENGINE_SAMPLE)
    ap.add_argument("--dry-run", action="store_true",
                    help="log findings without writing brain_proposals")
    a = ap.parse_args()
    sys.exit(main(a.since, a.min_sample, a.dry_run))
