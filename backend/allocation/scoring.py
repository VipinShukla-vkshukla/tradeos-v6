"""
What a proposal is worth, per rupee-day, from evidence that was not selected.

    edge = E[R] / expected_hold_days

    R_target = (target - entry) / (entry - stop)
    risk_pct = (entry - stop) / entry
    cost_R   = full_round_trip(product, entry, qty) / risk_pct
    E[R]     = expectation over the EMPIRICAL R distribution of this class
               - cost_R

THREE RULES, ALL BINDING
------------------------
**`product` is mandatory.** Omitting it prices a delivery trade as intraday and
understates its cost roughly fivefold — measured on this account, CNC round
trips run 0.32-1.07% against MIS at a flat 0.11-0.21%. A scorer that defaulted
the product would systematically over-allocate to swing, which is the book
holding real money.

**Empirical distribution, never binary.** A target-or-stop model assigns a
runner the value of its target and nothing more, which systematically
undervalues exactly the trade class that pays for the losers. The distribution
keeps the right tail because it is made of realised outcomes, not of two points.

**Priors come from the FULL FIELD, never from executed trades.** Executed trades
inherit the old policy's selection: they are the plans that policy liked, scored
in the region that policy sampled, and using them to rank the plans it refused
is circular. The unbiased populations already exist —

    signal_output_daily   every daily plan's forward outcome, traded or not
    intraday_setups       every detection's resolution, taken or not

— and are tens of thousands of observations a year against the ~90 closed
trades this account has ever had.

NO ESTIMATE WITHOUT ITS n
-------------------------
Every number returned here carries the sample it came from and a standard error.
Below the floor the prior is NEUTRAL and flagged, never interpolated and never
borrowed from a neighbouring class. A fabricated prior is worse than no prior
because it is indistinguishable from a measured one downstream.
"""

from __future__ import annotations

import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_float, cfg_int, get_supabase


PAGE = 1000


@dataclass(frozen=True)
class Prior:
    """An empirical R distribution for one class, with its own uncertainty."""
    key:        str
    n:          int
    mean_r:     float
    median_r:   float
    stderr:     float
    p10:        float
    p90:        float
    trigger_rate: float | None = None    # swing only: how often the zone filled
    below_floor:  bool = False
    note:         str = ""

    @property
    def usable(self) -> bool:
        return not self.below_floor

    def describe(self) -> str:
        if self.below_floor:
            return f"{self.key}: NEUTRAL (n={self.n} below floor) — {self.note}"
        return (f"{self.key}: E[R]={self.mean_r:+.3f} ±{self.stderr:.3f} "
                f"(n={self.n}, median {self.median_r:+.3f}, p10 {self.p10:+.2f}, "
                f"p90 {self.p90:+.2f})")


def _dist(key: str, values: list[float], floor: int,
          trigger_rate: float | None = None) -> Prior:
    if len(values) < floor:
        return Prior(key, len(values), 0.0, 0.0, float("nan"), 0.0, 0.0,
                     trigger_rate, True,
                     f"needs {floor} observations to be trusted")
    s = sorted(values)
    return Prior(
        key      = key,
        n        = len(s),
        mean_r   = statistics.fmean(s),
        median_r = statistics.median(s),
        stderr   = statistics.stdev(s) / (len(s) ** 0.5) if len(s) > 1 else float("nan"),
        p10      = s[int(0.10 * (len(s) - 1))],
        p90      = s[int(0.90 * (len(s) - 1))],
        trigger_rate = trigger_rate,
    )


def intraday_priors(sb) -> dict[str, Prior]:
    """
    Per-engine R distributions from every detection, taken or refused.

    intraday_setups is the population the architecture calls this system's
    rarest asset: 595 detections on 04-Aug-2026, 595 resolved. `outcome_pct` is
    the realised move; dividing by the setup's own risk turns it into R so the
    two books share a scale.
    """
    floor = cfg_int("priors_min_sample_intraday", 30)
    rows, off = [], 0
    while True:
        page = (sb.table("intraday_setups")
                  .select("strategy,outcome,outcome_pct,entry,stop,direction")
                  .not_.is_("outcome_pct", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    # SEGMENTED BY DIRECTION, NOT POOLED.
    #
    # `stop < entry` excluded every short row outright, so a short engine would
    # have accumulated detections forever and never acquired a prior — it would
    # have sat permanently on the NEUTRAL fallback and been scored as though it
    # had no history, however much it had.
    #
    # Pooling them instead would be worse than excluding them. A long and a
    # short in the same name are not two samples of one distribution: they have
    # different base rates (indices drift up, so the long side has a tailwind
    # the short side pays for), different failure modes (a short squeezes, a
    # long does not) and different tails. `outcome_pct` is also signed against
    # the LONG convention by the resolver, so a short's realised R is its
    # negation. Keyed as "ENGINE/SHORT" so the two never average together.
    from intraday import direction as D

    by: dict[str, list[float]] = {}
    for r in rows:
        try:
            entry, stop = float(r.get("entry") or 0), float(r.get("stop") or 0)
            d = D.normalise(r.get("direction"))
            risk = D.risk_per_share(entry, stop, d) if entry else 0.0
            risk_pct = risk / entry * 100.0 if risk else 0.0
            if risk_pct <= 0:
                continue
            key = r["strategy"] or "?"
            if D.is_short(d):
                key = f"{key}/SHORT"
            by.setdefault(key, []).append(float(r["outcome_pct"]) / risk_pct)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    out = {k: _dist(f"INTRADAY/{k}", v, floor) for k, v in by.items()}
    if rows:
        # The book-level fallback is ALSO split. `INTRADAY/ALL` is what a class
        # with too few observations falls back to, so pooling shorts into it
        # would contaminate the long fallback with a different distribution —
        # the exact borrowing this module's own header forbids ("never borrowed
        # from a neighbouring class"). A short with no prior falls back to the
        # SHORT book, not to the book as a whole.
        longs  = [x for k, v in by.items() if not k.endswith("/SHORT") for x in v]
        shorts = [x for k, v in by.items() if k.endswith("/SHORT") for x in v]
        out["INTRADAY/ALL"] = _dist("INTRADAY/ALL", longs, floor)
        out["INTRADAY/ALL/SHORT"] = _dist("INTRADAY/ALL/SHORT", shorts, floor)
    return out


# Swing engine -> family, matching docs/0_SYSTEM_BLUEPRINT.md §4: CTL, SEC,
# TPO, SBS, VBD, RSB and IAD share family CONTINUATION; MOM, RVS, PEAD and ACC
# are each split into their own family because the blueprint's own reason for
# splitting them — a distinct evidence profile, tracked SHADOW/ACTIVE
# separately — is exactly what pooling them back into CONTINUATION would erase.
_SWING_FAMILY = {
    "CTL": "CONTINUATION", "SEC": "CONTINUATION", "TPO": "CONTINUATION",
    "SBS": "CONTINUATION", "VBD": "CONTINUATION", "RSB": "CONTINUATION",
    "IAD": "CONTINUATION",
    "MOM": "MOM", "RVS": "RVS", "PEAD": "PEAD", "ACC": "ACC",
}


def swing_family(strategy: str | None) -> str:
    """
    `strategy` (written as `strategy_source` earlier in the pipeline — see
    `screen_stocks.py::run_sector_rotation` and friends) is a '+'-joined combo
    of every engine that agreed on a name that day, e.g. "CTL+SEC" or
    "CTL+MOM+SEC" — confirmed empirically against `signal_output_daily`:
    dozens of distinct combo strings, most with single-digit counts, which is
    too fine a grain for a stable prior on its own.

    A combo stays CONTINUATION only if EVERY constituent is a CONTINUATION
    engine. If any constituent belongs to a family the blueprint deliberately
    isolated (MOM, RVS, PEAD, ACC), that family wins — a mixed signal must
    never be silently diluted back into the pool it was split out of. Ties
    among more than one non-CONTINUATION family (not observed in current
    data) resolve alphabetically, deterministic rather than order-of-arrival.

    Unrecognised or empty resolves to "ALL", the existing book-level fallback
    key — never invented, matching this module's own rule for the R
    distributions themselves.

    A trailing parenthetical annotation is stripped before the lookup —
    found 07-Aug-2026, closed_positions.strategy carrying "CTL (Legacy)" for
    21 of 75 historical SWING closes (89% of the sample together with plain
    "CTL"), which `_SWING_FAMILY.get()` cannot match as written and which
    resolved to "ALL" instead of CONTINUATION — a real family's engine
    silently falling out of its own bucket, the identical shape as the
    signal_type/strategy column-confusion bug this module's own swing_
    priors() docstring already documents. "CTL (Legacy)" is CTL; the
    parenthetical is metadata about WHEN it was labelled, not a different
    engine. Generic strip (any trailing "(...)"), not a hardcoded alias list,
    so a future "SEC (Legacy)" or similar is not a second silent miss.
    """
    strip_paren = lambda p: re.sub(r"\s*\([^)]*\)\s*$", "", p).strip()
    parts = [strip_paren(p) for p in (strategy or "").split("+") if p]
    parts = [p for p in parts if p]
    if not parts:
        return "ALL"
    families = {_SWING_FAMILY.get(p, "ALL") for p in parts}
    families.discard("CONTINUATION")
    if not families:
        return "CONTINUATION"
    if len(families) == 1:
        return next(iter(families))
    return sorted(families)[0]


def swing_priors(sb) -> dict[str, Prior]:
    """
    R distributions from every daily plan's forward outcome, traded or not.

    SEGMENTED BY ENGINE FAMILY (`swing_family(strategy)`), NOT `signal_type`.

    06-Aug-2026: this function keyed its buckets on `signal_type`, which reads
    as "which engine produced this" but is actually a WORKFLOW status —
    confirmed against real data, its values are WATCH, BUY_CANDIDATE,
    REENTRY_SETUP, AVOID_ENTRY_EVENT, MOMENTUM_CONTINUATION, never an engine
    name. The real engine identity lives in the `strategy` column (CTL, SEC,
    MOM, TPO, ... and their '+'-joined combos). This was invisible from the
    allocator's own logs because it compounded with a second, independent bug
    at the call site (`proposal.py::from_swing`, `intraday/engine.py`
    `_allocate_shadow`): every swing Proposal's `source` was ALSO always the
    literal fallback string "CONTINUATION" regardless of which engine fired,
    so the class lookup `pri.get(f"SWING/{p.source}")` was `pri.get(
    "SWING/CONTINUATION")` against a dict that never contained that key
    either way — always missing, always falling through to the pooled
    SWING/ALL prior, for every swing candidate, regardless of engine. Fixed
    together: `from_swing` now reads `strategy` (not the never-populated
    `strategy_source`) through `swing_family()`, the same function this uses
    to build the keys it looks up.

    SEGMENTED BY WHETHER THE ENTRY LEVEL WAS REACHED, NOT POOLED.

    A plan's forward outcome assumes it *could* have been entered at its
    recorded level. Where the zone was never touched that assumption is
    untested, so pooling triggered and untriggered plans into one mean answers a
    question nobody asked. The trigger rate is reported separately and the R
    distribution is built only from plans that actually filled.

    Plans without a recorded stop cannot be expressed in R at all — 1,246 of
    1,711 were written before planned_stop was populated. Their forward return
    is real and is reported as a percentage, but it is NOT converted to R
    against an invented denominator.
    """
    floor = cfg_int("priors_min_sample_swing", 30)
    rows, off = [], 0
    while True:
        page = (sb.table("signal_output_daily")
                  .select("strategy,outcome_category,outcome_return_pct,"
                          "outcome_entered,entry_zone_high,planned_stop")
                  .not_.is_("outcome_category", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    if not rows:
        return {}

    entered = [r for r in rows if r.get("outcome_entered")]
    trigger = len(entered) / len(rows)

    by: dict[str, list[float]] = {}
    for r in entered:
        entry, stop = r.get("entry_zone_high"), r.get("planned_stop")
        ret = r.get("outcome_return_pct")
        if None in (entry, stop, ret):
            continue                      # no stop → no R, and none is invented
        try:
            entry, stop = float(entry), float(stop)
            risk_pct = (entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            by.setdefault(swing_family(r.get("strategy")), []).append(
                float(ret) / risk_pct)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    out = {k: _dist(f"SWING/{k}", v, floor, trigger) for k, v in by.items()}
    allr = [x for v in by.values() for x in v]
    out["SWING/ALL"] = _dist("SWING/ALL", allr, floor, trigger)
    return out


def expected_hold_days(sb, framework: str) -> tuple[float, int]:
    """
    Measured, per book, from closed records. Never hardcoded.

    Returns (days, n) so the caller can see how thin the estimate is. With ~19
    intraday and ~72 swing closes the intraday figure is weak, and the readiness
    review says to report that uncertainty rather than present a point estimate.
    """
    rows = (sb.table("closed_positions").select("hold_days")
              .eq("framework", framework.upper())
              .not_.is_("hold_days", "null").limit(PAGE).execute().data) or []
    days = [max(float(r["hold_days"]), 0.5) for r in rows]   # same-day = half a day
    if not days:
        return 1.0, 0
    return statistics.fmean(days), len(days)


def score(entry: float, stop: float, target: float, qty: int, product: str,
          prior: Prior, hold_days: float, direction: str = "LONG") -> dict:
    """
    One proposal, on the common scale. Pure arithmetic over in-memory data.

    `product` is a required positional argument on purpose — there is no default
    that is safe for both books.

    `direction` defaults to LONG because every caller predating shorts is one.
    The coherence test was `stop >= entry or target <= entry` — which is the
    definition of a correctly constructed SHORT. So every short proposal
    returned edge=None, and `policies.intraday_stopping` renders a None edge as
    "not scoreable — levels incoherent or no prior" and DECLINEs it. Shorts
    would have been refused by the allocator before any engine's opinion was
    consulted, and the refusal would have read like a data problem rather than
    a policy.
    """
    from intraday.cost_model import round_trip
    from intraday import direction as D

    if qty <= 0:
        return {"edge": None, "reason": "incoherent levels — no position"}
    ok, why = D.validate(entry, stop, target, direction)
    if not ok:
        return {"edge": None, "reason": f"incoherent levels — {why}"}

    risk  = D.risk_per_share(entry, stop, direction)
    risk_pct = risk / entry
    r_target = D.reward_per_share(entry, target, direction) / risk
    friction = round_trip(entry, qty, product=product).total
    cost_r   = friction / (risk_pct * entry * qty)

    if prior.usable:
        e_r, basis = prior.mean_r - cost_r, f"empirical n={prior.n}"
    else:
        # NEUTRAL means zero expected R, not "assume the target". Flagged so a
        # caller cannot mistake an absent prior for a measured one.
        e_r, basis = 0.0 - cost_r, f"NEUTRAL prior (n={prior.n} below floor)"

    return {
        "edge":        e_r / max(hold_days, 0.5),
        "e_r":         e_r,
        "cost_r":      cost_r,
        "r_target":    r_target,
        "risk_pct":    risk_pct,
        "friction":    friction,
        "hold_days":   hold_days,
        "prior_n":     prior.n,
        "prior_floor": prior.below_floor,
        "basis":       basis,
    }


def _swing_bias_warning(sb) -> None:
    """
    Say out loud why the swing R prior is not yet trustworthy.

    IT LOOKS POSITIVE AND IT IS BIASED IN A KNOWN DIRECTION.

    A plan enters the R distribution only once it has resolved to TARGET or
    STOP, which requires a recorded stop AND a level being reached. Two things
    follow, both inflating the mean:

      · planned_stop was only populated from 28-Jul-2026, so the entire R
        sample is drawn from a few recent weeks and one market regime.
      · a plan that hits its target in three sessions resolves; one that grinds
        for fifteen is still pending. In a young dataset the fast winners have
        resolved and the slow outcomes have not — survivorship in TIME, not in
        selection, and it disappears only as windows close.

    Meanwhile the 625 plans with no recorded levels average **-0.86%** over the
    same horizon, and the expectancy ledger puts the realised swing book at
    **-Rs 12,489** across 72 closed trades. A +0.5R prior sitting next to those
    two facts is a warning, not a result.
    """
    try:
        rows = (sb.table("signal_output_daily")
                  .select("date,outcome_category,planned_stop")
                  .not_.is_("outcome_category", "null").limit(PAGE).execute().data) or []
        scored = [r for r in rows if r["outcome_category"] in ("TARGET", "STOP")]
        if not scored:
            return
        span = f"{min(str(r['date']) for r in scored)} to {max(str(r['date']) for r in scored)}"
        unscored = len(rows) - len(scored)
        logger.warning(f"  ⚠ the R distribution above rests on {len(scored)} plans, all "
                       f"signalled between {span}.")
        logger.warning(f"    {unscored} further resolved plans have NO recorded stop and are "
                       f"excluded from R — their mean forward return is negative.")
        logger.warning(f"    Fast outcomes resolve first, so a young sample over-represents "
                       f"quick winners. Do not promote anything on this prior until the "
                       f"15-session windows have closed across a full quarter.")
    except Exception:
        pass


def tercile_report(sb=None) -> int:
    """
    DIAGNOSTIC ONLY — not called from `swing_priors()`, not wired into the
    allocator, changes nothing. Run with `python -m allocation.scoring --tercile`.

    Council Break 2 observed, correctly, that priors discarded `final_score`
    entirely. Whether conditioning on it actually predicts anything is a
    separate, empirical question, and `docs/TRADING_METHODOLOGY_REVIEW.md`
    already warns that tercile-mining at this account's sample size produces
    spurious "material" splits by chance. This prints mean R for each
    `(swing_family(strategy), final_score tercile)` bucket next to the pooled
    per-family number, so the split can be read by eye before anyone
    conditions priors on it.

    MEASURED 06-Aug-2026, n=125 entered+resolved CONTINUATION plans (the
    dominant family): tercile means were 0.5156 / 0.4910 / 0.5112 — FLAT, no
    monotonic separation. `final_score`'s hand-set weights
    (`compute_msl.py::get_final_score_weights`,
    0.22/0.20/0.15/0.13/0.12/0.08/0.06/0.04) have never been validated
    against forward outcomes, and this is the first time they were tested
    against one: on this measurement they carry no information about forward
    R once a plan has already entered. MOM and RVS showed n too small (17 and
    5) to read anything into. Conclusion: the conditioning was NOT shipped.
    Re-run this as the sample grows — a flat result at n=125 is evidence, not
    a permanent verdict.
    """
    floor = cfg_int("priors_min_sample_swing", 30)
    sb = sb or get_supabase()
    rows, off = [], 0
    while True:
        page = (sb.table("signal_output_daily")
                  .select("strategy,outcome_return_pct,outcome_entered,"
                          "entry_zone_high,planned_stop,final_score,ai_tier")
                  .not_.is_("outcome_category", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    triples: list[tuple[str, float, float, str]] = []   # (family, final_score, R, ai_tier)
    for r in rows:
        if not r.get("outcome_entered"):
            continue
        entry, stop = r.get("entry_zone_high"), r.get("planned_stop")
        ret, fs = r.get("outcome_return_pct"), r.get("final_score")
        if None in (entry, stop, ret, fs):
            continue                      # no stop or no score → excluded, not invented
        try:
            entry, stop = float(entry), float(stop)
            risk_pct = (entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            triples.append((swing_family(r.get("strategy")), float(fs),
                            float(ret) / risk_pct, r.get("ai_tier") or "UNTIERED"))
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    logger.info("═" * 74)
    logger.info("TERCILE REPORT — mean R by (engine, final_score tercile). DIAGNOSTIC ONLY.")
    logger.info("Not wired into the live allocator or swing_priors(). Evidence for a decision,")
    logger.info("not the decision.")
    logger.info("═" * 74)

    if not triples:
        logger.error("  no entered plan has both a stop and a final_score — nothing to report")
        return 1

    by_engine: dict[str, list[tuple[float, float]]] = {}
    for eng, fs, r, _tier in triples:
        by_engine.setdefault(eng, []).append((fs, r))

    for eng in sorted(by_engine):
        pts = sorted(by_engine[eng])          # sorted by final_score
        n = len(pts)
        pooled = _dist(f"SWING/{eng}", [r for _, r in pts], floor)
        logger.info("")
        logger.info(f"── {eng} (n={n}) ──")
        logger.info(f"  pooled: {pooled.describe()}")
        if n < floor:
            logger.warning(f"  too few observations for a tercile split "
                           f"(n={n}, floor={floor} per bucket recommended)")
            continue
        t1, t2 = pts[n // 3][0], pts[(2 * n) // 3][0]
        for label, vals in (
            ("LOW",  [r for fs, r in pts if fs <= t1]),
            ("MID",  [r for fs, r in pts if t1 < fs <= t2]),
            ("HIGH", [r for fs, r in pts if fs > t2]),
        ):
            d = _dist(f"SWING/{eng}/{label}", vals, floor)
            logger.info(f"  {label:<4}: {d.describe()}")

    # Same population, sliced by ai_decision_engine's tier instead of
    # final_score. Pooled across engine families — splitting by both would
    # starve every bucket at this sample size.
    logger.info("")
    logger.info("── mean R by ai_tier, pooled across engines ──")
    by_tier: dict[str, list[float]] = {}
    for _eng, _fs, r, tier in triples:
        by_tier.setdefault(tier, []).append(r)
    for tier in sorted(by_tier):
        d = _dist(f"SWING/TIER/{tier}", by_tier[tier], floor)
        (logger.warning if d.below_floor else logger.info)(f"  {tier:<14}: {d.describe()}")

    return 0


def report() -> int:
    """Print every prior the system can currently justify, with its n."""
    sb = get_supabase()
    logger.info("═" * 74)
    logger.info("EMPIRICAL PRIORS — from the full field, never from executed trades")
    logger.info("═" * 74)

    for name, fn in (("INTRADAY — every detection", intraday_priors),
                     ("SWING — every daily plan", swing_priors)):
        logger.info("")
        logger.info(f"── {name} ──")
        priors = fn(sb)
        if not priors:
            logger.error("  no resolved outcomes — this population is EMPTY")
            continue
        for k in sorted(priors):
            p = priors[k]
            (logger.warning if p.below_floor else logger.info)(f"  {p.describe()}")
        any_p = next(iter(priors.values()))
        if any_p.trigger_rate is not None:
            logger.info(f"  trigger rate (zone actually filled): {any_p.trigger_rate:.0%}")
            _swing_bias_warning(sb)

    logger.info("")
    logger.info("── Expected hold days, measured per book ───────────────────────────")
    for fw in ("SWING", "INTRADAY"):
        d, n = expected_hold_days(sb, fw)
        warn = "  ← thin, treat as provisional" if n < 30 else ""
        logger.info(f"  {fw:<9} {d:.2f} days from n={n} closed records{warn}")
    return 0


if __name__ == "__main__":
    sys.exit(tercile_report() if "--tercile" in sys.argv else report())
