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
                  .select("strategy,outcome,outcome_pct,entry,stop")
                  .not_.is_("outcome_pct", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    by: dict[str, list[float]] = {}
    for r in rows:
        try:
            entry, stop = float(r.get("entry") or 0), float(r.get("stop") or 0)
            risk_pct = (entry - stop) / entry * 100.0 if entry and stop and stop < entry else 0.0
            if risk_pct <= 0:
                continue
            by.setdefault(r["strategy"] or "?", []).append(float(r["outcome_pct"]) / risk_pct)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    out = {k: _dist(f"INTRADAY/{k}", v, floor) for k, v in by.items()}
    if rows:
        allr = [x for v in by.values() for x in v]
        out["INTRADAY/ALL"] = _dist("INTRADAY/ALL", allr, floor)
    return out


def swing_priors(sb) -> dict[str, Prior]:
    """
    R distributions from every daily plan's forward outcome, traded or not.

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
                  .select("signal_type,outcome_category,outcome_return_pct,"
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
            by.setdefault(r.get("signal_type") or "ALL", []).append(float(ret) / risk_pct)
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
          prior: Prior, hold_days: float) -> dict:
    """
    One proposal, on the common scale. Pure arithmetic over in-memory data.

    `product` is a required positional argument on purpose — there is no default
    that is safe for both books.
    """
    from intraday.cost_model import round_trip

    if entry <= 0 or stop >= entry or target <= entry or qty <= 0:
        return {"edge": None, "reason": "incoherent levels"}

    risk_pct = (entry - stop) / entry
    r_target = (target - entry) / (entry - stop)
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
    sys.exit(report())
