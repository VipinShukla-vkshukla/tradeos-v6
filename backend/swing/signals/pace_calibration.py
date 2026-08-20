"""
Per-engine-family PACE calibration for the swing exit ladder's stall clock.

WHY THIS EXISTS
----------------
The stall exit (control/position_lifecycle.py::evaluate_exit) asks one
question with ONE number for the entire book: "has this position shown real
progress within exit_stall_days sessions?" That number (10) was picked once,
from a legacy manually-sized book, and applies identically to a CTL breakout
and a MOM momentum-continuation trade even though they resolve on visibly
different clocks. Measured 21-Aug-2026 from every plan swing/signals/
outcomes.py has resolved as TARGET (i.e. every winner on record, whether or
not it was ever traded):

    family          n     median days-to-target   p75
    CONTINUATION   296             3                6
    MOM             86             4                7
    RVS              6         (too thin for its own number)

A CONTINUATION trade whose own family's winners resolve inside 6 sessions
three times out of four gains nothing from a flat 10-session leash — those
extra four sessions are capital sitting in something that, on its own
family's evidence, has already told you what it is. This recalibrates the
EXISTING stall_days input per family, from the same resolved-outcome stream
swing_priors() already reads. It does not invent a new exit rule.

WHY THIS CAN ONLY TIGHTEN THE CLOCK, NEVER LOOSEN IT
------------------------------------------------------
Capped at the operator's own configured `exit_stall_days`. A family whose
own winners take LONGER than the global default is left AT the global
default rather than given more rope. Tightening a live account's loss-side
rule without a shadow-observation period is a direction this project's own
"loss-side rules stay strict and unconditional" principle already treats as
safe to automate; loosening one is not, so this never does. Every fallback
— a thin sample, an unrecognised family, a fetch failure — resolves to that
same global default: current behaviour, unchanged, is always the floor this
degrades to.

RECALIBRATES ITSELF, WITH NO CODE CHANGE, AS MORE PLANS RESOLVE
------------------------------------------------------------------
`swing/signals/outcomes.py` resolves more of `signal_output_daily` every
session (70.0% of 2,720 plans resolved as of 20-Aug-2026, growing daily).
This is queried fresh once per daemon start (see `intraday/engine.py`'s
`self._policy` cache, the same lifetime `load_exit_policy()` already has) —
the family clocks sharpen as evidence accumulates, with no manual
recalibration required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config import cfg_int


def build_family_stall_days(sb, *, min_sample: int | None = None,
                            global_default: int = 10,
                            floor_days: int | None = None) -> dict[str, int]:
    """
    {family: calibrated_stall_days} for every family with enough resolved
    TARGET outcomes to trust a p75. A family below the sample floor is
    simply absent from the dict — the caller's own fallback (the flat
    `global_default`) covers it, exactly as the stall rule always has.

    min_sample/floor_days default to system_config
    (swing_stall_pace_min_sample=20, swing_stall_pace_floor_days=3) when not
    passed explicitly, the same "cfg with a sane built-in default" pattern
    every other threshold in this exit ladder already follows.
    """
    if min_sample is None:
        min_sample = cfg_int("swing_stall_pace_min_sample", 20)
    if floor_days is None:
        floor_days = cfg_int("swing_stall_pace_floor_days", 3)

    try:
        from allocation.scoring import swing_family
    except Exception as e:
        logger.warning(f"  pace_calibration: swing_family unavailable — {e}")
        return {}

    try:
        # PAGED. signal_output_daily is a table known to exceed PostgREST's
        # 1000-row silent cap, and this query's own TARGET-only result set
        # will keep growing as swing/signals/outcomes.py resolves more of
        # the field — the whole point of this module is to sharpen with
        # more data, so "under 1000 today" is not a property to rely on.
        # No `id` column on this table (final_snapshot never wrote one);
        # (symbol, date) is its unique key, same order_by swing_priors()
        # already uses for the identical reason.
        from config import fetch_all
        rows = fetch_all(
            lambda: sb.table("signal_output_daily")
                      .select("strategy,outcome_hold_days,symbol,date")
                      .eq("outcome_entered", True)
                      .eq("outcome_category", "TARGET")
                      .not_.is_("outcome_hold_days", "null"),
            order_by="symbol,date")
    except Exception as e:
        logger.warning(f"  pace_calibration: fetch failed, stall clock stays "
                       f"at the flat default for every family — {e}")
        return {}

    return _calibrate(rows, swing_family, min_sample=min_sample,
                      global_default=global_default, floor_days=floor_days)


def _calibrate(rows: list[dict], family_fn, *, min_sample: int,
               global_default: int, floor_days: int) -> dict[str, int]:
    """
    Pure. `family_fn` is injected (rather than imported here) so this is
    testable with a fixture classifier and no dependency on a live import —
    the same separation `intraday_priors(sb, rows=...)` uses for its own
    I/O/compute split.
    """
    by_family: dict[str, list[int]] = {}
    for r in rows:
        d = r.get("outcome_hold_days")
        if d is None:
            continue
        try:
            d = int(d)
        except (TypeError, ValueError):
            continue
        if d < 0:
            continue
        fam = family_fn(r.get("strategy"))
        by_family.setdefault(fam, []).append(d)

    out: dict[str, int] = {}
    for fam, vals in by_family.items():
        if len(vals) < min_sample:
            continue
        s = sorted(vals)
        p75 = s[int(0.75 * (len(s) - 1))]
        out[fam] = max(floor_days, min(int(round(p75)), global_default))
    return out
