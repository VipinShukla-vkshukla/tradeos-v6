"""
Setup quality — a measurement, not a decision.

WHAT THIS IS FOR
----------------
The swing book's selection question is "which of tonight's ~60 plans deserve
tomorrow's five entries?" `analysis/entry_ranking.score_plan()` answers it
today. This module scores the same plans on a different, evidence-fitted basis
so the two can be compared on real forward outcomes.

NOTHING READS IT FOR A TRADING DECISION, and that is deliberate.

WHY NOT, GIVEN IT WAS BUILT TO IMPROVE SELECTION
------------------------------------------------
It was tested as a selection input, through current mechanics, on the book
simulation described in docs/FINDINGS.md (20-Sep-2026): each session, the plans
the live gate would buy, sized by the live sizer, at the live caps, managed by
the real evaluate_exit ladder on 15-minute bars — ranked by score_plan() in one
arm and by this score in the other, everything else identical.

    16-Aug..17-Sep holdout      n    win     sumR        net
    production ranker          55   38.2%   -5.55R    -48,426
    this score                 47   40.4%   -9.28R    -46,346

In-sample the same score looked excellent (+7.89R against the ranker's +2.40R,
67.6% win). That gap between in-sample and holdout is the signature of a fitted
score, not an edge. Trading on it would have cost money.

So it is written down every evening and read by nobody. The gate for revisiting:
it must beat the production ranker on a window it was not fitted on, at the live
caps. Storing it is what makes that test possible with new data rather than with
the data it was built from.

HOW THE FACTORS WERE CHOSEN
---------------------------
Every column of signal_log, master_shortlist, sector_strength and
industry_strength was scanned against signal_outcomes.ret_fwd_5d — a
mechanics-free outcome, no exit policy in it. A factor is kept only if its
top-third-minus-bottom-third spread points the SAME WAY in all three of: the
first half of the fit window, the second half, and the pooled fit window.
Nothing dated 16-Aug-2026 or later informed the selection, the signs or the
centres.

`sector_avg_ret_6m` was dropped by that rule: -0.25% / -0.57% in the halves but
+0.00% pooled. A factor whose direction flips between a window and its own
halves has no direction to encode. `market_cap` and `low_52w` survived the
statistics and were excluded by type — they are rupee levels, not scale-free
quantities, and what they actually measure is "smaller and cheaper", which is a
size factor rather than a property of the setup.

WHAT THE SURVIVING FACTORS SAY
------------------------------
Seven of eleven are sector or industry context, and they do not all point the
same way. Hot sectors score BADLY (avg_ret_1m, avg_rs_vs_nifty, avg_rsi_weekly
all negative; sector_rank_at_entry POSITIVE, meaning plans from lower-ranked
sectors did better) while broad ones score WELL (breadth_score,
composite_score). Read together: broad but not hot.

`days_to_trigger_est` is the largest single effect in the set (+3.25% pooled,
+4.11% / +3.80% in the halves) and the most easily misread. It is measured from
the SIGNAL date on every plan, entered or not — a plan that needs another four
days to reach its trigger is not a trade yet. It says something about which
plans the screener should prefer; it does not say the book would have captured
it. This is exactly the difference between a signal-level statistic and a
book-level result, and exactly why this module is instrumentation.

PURE BY CONSTRUCTION
--------------------
No database, no config, no clock — a dict in, a Quality out, so tools/verify.py
exercises it offline. Callers assemble the features; this module never fetches
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CLIP = 3.0          # z-scores beyond this are capped, so one outlier cannot
                    # swing an eleven-factor mean on its own
MIN_FACTORS = 8     # below this the score is None: a partial score and a full
                    # one are not the same measurement and must not be compared


@dataclass(frozen=True)
class Factor:
    """
    One input. `sign` is +1 when higher is better, -1 when lower is better.

    `centre` is the median and `scale` the population standard deviation over
    the 4,887 signals dated before 2026-08-16 — fixed numbers, not re-fitted at
    runtime, so tonight's score means the same thing as last week's. Refresh
    them deliberately, with a note saying what changed, never silently.
    """
    name: str
    sign: int
    centre: float
    scale: float


# Fitted on signals dated before 2026-08-16. The comment on each line is that
# factor's own top-third-minus-bottom-third forward 5-day return in the pooled
# fit window — the effect being encoded, in the units the operator reads.
FACTORS: tuple[Factor, ...] = (
    Factor("sector_avg_ret_1m",      -1,  4.0920,  4.7502),   # -0.50%
    Factor("industry_avg_ret_1m",    -1,  4.6020,  5.4807),   # -0.95%
    Factor("dist_vwap_20d_pct",      -1,  8.0300,  6.7906),   # -0.59%
    Factor("ret_12m",                +1, 23.3670, 33.7286),   # +0.31%
    Factor("sector_composite_score", +1,  0.7109,  0.1406),   # +0.51%
    Factor("days_to_trigger_est",    +1,  3.0000,  2.3992),   # +3.25%
    Factor("sector_avg_rsi_weekly",  -1, 59.6290,  3.3831),   # -0.69%
    Factor("sector_rank_at_entry",   +1,  3.0000,  2.6869),   # +1.53%
    Factor("sector_avg_rs_vs_nifty", -1,  2.9133,  4.2626),   # -0.78%
    Factor("sector_breadth_score",   +1,  0.8179,  0.1945),   # +0.79%
    Factor("base_score",             -1, 79.2000, 12.9498),   # -0.28%
)


@dataclass
class Quality:
    total: float | None                      # None when too few factors are present
    components: dict = field(default_factory=dict)
    n_used: int = 0
    missing: tuple = ()

    def as_row(self) -> dict:
        """The two columns signal_output_daily stores."""
        return {
            "setup_quality": None if self.total is None else round(self.total, 4),
            "setup_quality_components": {
                "n_used": self.n_used,
                "missing": list(self.missing),
                "z": {k: round(v, 3) for k, v in self.components.items()},
            },
        }


def _num(v) -> float | None:
    if isinstance(v, bool):      # a bool is an int in Python and would score as 0/1
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN is not a measurement


def score(features: dict) -> Quality:
    """
    Score one plan. `features` is keyed by Factor.name; absent or unusable
    values are skipped, not defaulted.

    A missing factor must not read as an average one. Substituting the centre
    would make "we did not measure this" and "we measured it and it was
    typical" the same number — the failure this repo has already paid for in
    the allocator's cold start.
    """
    z, used, comps, missing = 0.0, 0, {}, []
    for f in FACTORS:
        v = _num(features.get(f.name))
        if v is None:
            missing.append(f.name)
            continue
        raw = (v - f.centre) / (f.scale or 1.0)
        c = f.sign * max(-CLIP, min(CLIP, raw))
        comps[f.name] = c
        z += c
        used += 1
    total = (z / used) if used >= MIN_FACTORS else None
    return Quality(total=total, components=comps, n_used=used, missing=tuple(missing))


def merge_plan_rows(signal: dict | None, shortlist: dict | None) -> dict:
    """
    One plan, from the two tables that carry its columns.

    signal_output_daily — the evening snapshot — does NOT carry
    dist_vwap_20d_pct, ret_12m or base_score; those live on signal_log and
    master_shortlist. A caller holding only the snapshot row scores 8 of 11
    factors at best and often fewer, which is why this merge is shared rather
    than written out at each call site: the writer and the study must assemble
    the same plan or their numbers are not comparable.

    signal_log wins where it has a value, because it is the row the evening
    pipeline actually decided from.
    """
    merged = dict(shortlist or {})
    merged.update({k: v for k, v in (signal or {}).items() if v is not None})
    return merged


def features_from(plan: dict, sector: dict | None = None,
                  industry: dict | None = None) -> dict:
    """
    Assemble the feature dict from rows the evening pipeline already holds: a
    plan row (signal_log / master_shortlist / signal_output_daily shapes all
    carry these keys) plus that plan's sector_strength and industry_strength
    rows.

    Kept here rather than in the writer so the mapping is testable offline and
    a column rename shows up in one place.
    """
    s = sector or {}
    i = industry or {}
    return {
        "sector_avg_ret_1m":      s.get("avg_ret_1m"),
        "industry_avg_ret_1m":    i.get("avg_ret_1m"),
        "dist_vwap_20d_pct":      plan.get("dist_vwap_20d_pct"),
        "ret_12m":                plan.get("ret_12m"),
        "sector_composite_score": s.get("composite_score"),
        "days_to_trigger_est":    plan.get("days_to_trigger_est"),
        "sector_avg_rsi_weekly":  s.get("avg_rsi_weekly"),
        "sector_rank_at_entry":   plan.get("sector_rank_at_entry") or s.get("rank"),
        "sector_avg_rs_vs_nifty": s.get("avg_rs_vs_nifty"),
        "sector_breadth_score":   s.get("breadth_score"),
        "base_score":             plan.get("base_score"),
    }
