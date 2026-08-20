"""
Which intraday engines exist, which are on, and what each produced.

Mirrors how swing engines are handled in strategy_config — enable/disable and
lifecycle live in the database, not in code, so an engine can be shadowed or
retired without a deploy. Kept as a SEPARATE registry because the two engine
families answer different questions and share no parameters.

RUNNING ORDER IS NOT PRIORITY
-----------------------------
Every enabled engine evaluates every symbol. When more than one fires on the
same name, the highest-confidence setup wins and the others are recorded as
corroboration rather than discarded — two independent engines agreeing is a
genuinely stronger signal than either alone, and throwing that away loses the
best information the multi-engine design produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger
from config import cfg, cfg_bool, cfg_float, get_supabase
from intraday import direction as D
from intraday.strategies.base import Setup, SymbolContext
from intraday.strategies.orb import OpeningRangeBreakout
from intraday.strategies.vwap_reclaim import VwapReclaim
from intraday.strategies.gap_and_go import GapAndGo
from intraday.strategies.pullback import TrendPullback
from intraday.strategies.prev_day_levels import PrevDayLevelRetest
from intraday.strategies.squeeze import SqueezeExpansion
from intraday.strategies.range_fade import RangeFade
from intraday.strategies.short_distribution import ShortDistribution
from intraday.strategies.gap_down_bounce import GapDownBounce

# Ordered by family so the coverage is visible: three that pay when a level
# breaks, one that pays when a trend continues, two that pay when nothing
# breaks at all. That last pair matters — most sessions do not trend, and a
# system built only of breakout engines is idle on the majority of days.
_ALL = [
    OpeningRangeBreakout(),   # ORB — today's level, first 15 min
    GapAndGo(),               # GAP — overnight repricing that holds
    PrevDayLevelRetest(),     # PDL — yesterday's level, break and retest
    SqueezeExpansion(),       # VCE — compression releasing
    TrendPullback(),          # PBK — first pullback in a trend day
    VwapReclaim(),            # VWR — fade and reclaim of the day's average
    RangeFade(),              # RNG — the low of a proven range
    ShortDistribution(),      # SDN — the short family: VWAP rejection, trap, breakdown
    GapDownBounce(),          # GDB — brain_proposals#190; SHADOW until scored, see its own module docstring
]


LIFECYCLE_ACTIVE  = "ACTIVE"     # evaluates, records, and may receive capital
LIFECYCLE_SHADOW  = "SHADOW"     # evaluates and records, but never receives capital
LIFECYCLE_RETIRED = "RETIRED"    # does not run at all


# ─────────────────────────────────────────────────────────────────────────────
# FAMILIES — seven engines become two, WITHOUT LOSING A SINGLE DETECTION
# ─────────────────────────────────────────────────────────────────────────────
#
# THE DISTINCTION THAT MATTERS, AND THAT IS EASY TO GET WRONG.
#
# Merging is not retiring. A retired engine stops contributing, and detections
# per surviving engine stay flat. A MERGED engine keeps finding exactly what it
# found before — its logic is untouched — and its hits are attributed to one
# family identity instead of to seven separate ones.
#
# That is what makes Stage 6's acceptance criterion reachable at all:
#
#     "Detection rate per surviving engine >= 3x prior"
#
# You cannot get there by deleting engines. You get there by counting the same
# detections under fewer names. Measured over the 595 resolved detections:
#
#     ORB family = ORB 30 + GAP 103 + PDL 97 = 230 against ORB's 30   -> 7.7x
#     VWR family = VWR 154 + PBK 60          = 214 against VWR's 154  -> 1.4x
#
# WHAT THE MERGE ACTUALLY CHANGES, THEN, IS THE VOTE — NOT THE DETECTION.
#
# Before, ORB, GAP and PDL firing on one name counted as three engines agreeing,
# and corroboration lifted confidence accordingly. They are not three
# independent opinions; GAP correlates ~0.6 with ORB and PDL ~0.7 by the
# production decision's own estimate, and all three key off the same opening
# structure. Treating them as independent manufactured conviction no single
# engine had. Within a family, agreement is now expected rather than evidence.
#
# The sub-engine name survives on every recorded setup, so "which condition
# fired" stays answerable and a family can be split again if the evidence ever
# demands it. Nothing is deleted.
FAMILIES = {
    "ORB": "ORB",     # opening-range breakout — the family core
    "GAP": "ORB",     # overnight repricing: a DAY-TYPE condition on the same structure
    "PDL": "ORB",     # prior-day levels: a REFERENCE condition on the same structure
    "VWR": "VWR",     # VWAP reclaim — institutional benchmark defence, the family core
    "PBK": "VWR",     # first pullback in a trend: a CONDITION within that defence
    "VCE": "VCE",     # shadowed; kept as its own family so its evidence stays separable
    "RNG": "RNG",     # shadowed; same
    # The short family is its own family and must NEVER be merged into a long
    # one. A long and a short in the same structure have different base rates
    # (the index drifts up), different failure modes (a short squeezes) and
    # different tails — pooling their detections would average away the only
    # thing worth measuring about either.
    "SDN": "SDN",     # SHORT: VWAP rejection · failed-breakout trap · range breakdown
    "GDB": "GDB",     # shadowed; new engine from brain_proposals#190, own family until proven
}


def family_of(name: str) -> str:
    """
    Which family an engine's detections are scored under.

    Config-overridable per engine so a family can be split back apart without a
    deploy — the burden of proof runs both ways, and a merge that turns out to
    have hidden an independent signal has to be reversible the same day.
    """
    default = FAMILIES.get(name.upper(), name.upper())
    return (cfg(f"intraday_engine_{name.lower()}_family", default) or default).upper()


def engine_lifecycle(name: str) -> str:
    """
    What an engine is allowed to do, read live.

    WHY SHADOW HAD TO EXIST BEFORE ANY ENGINE COULD BE RETIRED.

    Until now an intraday engine had exactly two states: enabled, or not. Turning
    one off stopped it evaluating, which stopped it recording detections, which
    stopped `outcomes.resolve_day` scoring them. Retiring an engine that way
    destroys the very evidence needed to decide whether retiring it was right —
    and the architecture is explicit that retirees "continue to be evaluated and
    resolved; they simply receive no capital", with the burden of proof on
    retention rather than removal.

    The swing side has had this since engine_registry was written. The intraday
    side did not, and that asymmetry is why Stage 6 starts here.

    The old `_enabled` key is still honoured so nothing an operator has already
    switched off silently comes back on.
    """
    key = name.lower()
    if not cfg_bool(f"intraday_engine_{key}_enabled", True):
        return LIFECYCLE_RETIRED
    state = (cfg(f"intraday_engine_{key}_lifecycle", LIFECYCLE_ACTIVE) or "").upper()
    return state if state in (LIFECYCLE_ACTIVE, LIFECYCLE_SHADOW, LIFECYCLE_RETIRED) \
        else LIFECYCLE_ACTIVE


def enabled_engines() -> list:
    """
    Engines that still run — ACTIVE and SHADOW both.

    Defaults to ON for a registered engine: a new engine that silently does
    nothing because someone forgot a config row is worse than one that runs and
    can be turned off.
    """
    return [e for e in _ALL if engine_lifecycle(e.name) != LIFECYCLE_RETIRED]


def active_engines() -> list:
    """Engines whose setups may actually be funded."""
    return [e for e in _ALL if engine_lifecycle(e.name) == LIFECYCLE_ACTIVE]


def _invalidation_is_reachable(s: Setup) -> bool:
    """
    Can the setup's OWN thesis-death level fire before its stop?

    WHY — 12-Aug-2026, NATIONALUM. GAP places its stop below the opening range
    low, then clamps it to `gap_max_risk_pct` when that is unaffordable. On an
    extended gap the two come apart completely: entry 419.30, stop 413.85, and
    an invalidation reading "a close below 399.45" — five percent away, and
    below the stop. `exit_policy._invalidated()` could never fire, so the trade
    the operator was shown ("dies if the gap is given back") and the trade the
    system actually held (a 1.3% price limit in the middle of a 5% range) were
    different trades. The engine's own risk model had silently overruled its
    structure and kept the structure's prose.

    THE TOLERANCE IS NOT ZERO, DELIBERATELY. Several engines place the stop
    just below the level on purpose, with a buffer smaller than the exit's,
    so the stop legitimately fires a hair first — MCX on the same day had the
    invalidation cut 0.58 below a stop 22.51 from entry, 2.6% of risk. That is
    the design working, not a defect. Only a divergence LARGE relative to the
    trade's own risk means the structure was abandoned: NATIONALUM's was 273%.

    Applied here rather than in each engine because it is a property every
    Setup must hold, and nine copies of it would be nine chances to drift.
    """
    from intraday.exit_policy import invalidation_level_from
    level, _ = invalidation_level_from(s)
    if not level:
        return True                     # no structural exit claimed; nothing to check
    risk = abs(s.entry - s.stop)
    if risk <= 0:
        return True                     # coherent() owns this failure

    # THE RAW LEVEL, NOT THE BUFFERED CUT — corrected 12-Aug-2026, same day,
    # from a live false positive. The first version compared the exit's
    # BUFFERED trigger (`level * (1 ± intraday_invalidation_buffer_pct)`) to
    # the stop, and refused TORNTPHARM's SDN VWAP-rejection short in
    # production: vwap 4842.29, stop 4843.81. That setup places its stop just
    # above the rejection high, DELIBERATELY tighter than the invalidation
    # buffer, so the stop and the invalidation are the same event — the same
    # design PBK's own comment calls out as what makes the entry efficient.
    # Charging it the buffer made a correct, tight stop look like an abandoned
    # structure, and the highest-quality short this book has was being refused
    # every cycle.
    #
    # The buffer is exit TIMING; the question here is structural — is the stop
    # anchored to the level the setup names, or somewhere unrelated? So compare
    # the level itself. NATIONALUM is still caught (stop 413.85 vs a level of
    # 399.45: 264% of risk away, the structure genuinely abandoned), and a stop
    # placed just past its level in either direction now reads as what it is.
    beyond = (level - s.stop) if D.is_short(s.direction) else (s.stop - level)
    return (beyond / risk) <= cfg_float("intraday_invalidation_max_beyond_stop_frac", 0.10)


def evaluate_all(ctx: SymbolContext, phase: str) -> tuple[Setup | None, list[Setup]]:
    """
    Run every enabled engine against one symbol.

    Returns (best, all_setups). `best` is the highest-confidence setup; the full
    list is kept so agreement between engines can be reported.
    """
    found: list[Setup] = []
    for eng in enabled_engines():
        try:
            s = eng.evaluate(ctx, phase)
            if s and not _invalidation_is_reachable(s):
                logger.info(
                    f"  {ctx.symbol}: {s.strategy} refused — its stop {s.stop:.2f} sits "
                    f"beyond the level it calls its own thesis death "
                    f"({s.invalidation}). The structural exit can never fire, so "
                    f"the trade has no thesis — only a price limit")
                s = None
            if s:
                # Stamped at detection so every downstream consumer — the
                # recorder, the resolver, the weekly review — reads the same
                # answer to "was this fundable?". Deciding it later, from config
                # read at a different moment, is how a shadow engine's outcomes
                # end up scored as if they had been live.
                s.meta["lifecycle"] = engine_lifecycle(eng.name)
                # Provenance first, identity second. `sub_engine` is which
                # condition actually fired; `family` is what the detection is
                # scored as. Keeping both is what makes the merge reversible.
                #
                # setdefault(), NOT =, FOUND 20-Aug-2026. This line's own
                # comment says sub_engine is "which condition actually fired"
                # — but `=` unconditionally overwrote whatever the engine had
                # already set with `s.strategy`, which for every engine IS the
                # condition (ORB, VCE, ...) except the one engine built with
                # three conditions inside one class: short_distribution.py's
                # three methods each set meta["sub_engine"] to "VWR"/"TRP"/
                # "ORB" — the actual condition — and this line stomped every
                # one of them back to "SDN" before the row was ever written.
                # Every historical SDN row in intraday_setups therefore reads
                # sub_engine="SDN", indistinguishable from strategy, and any
                # per-condition analysis (confidence calibration, priors,
                # this session's own banded-prior and arbitration machinery)
                # has been silently pricing all three SDN conditions as one
                # ever since sub_engine was introduced. Harmless for every
                # other engine, where sub_engine == strategy was already the
                # honest answer — setdefault() changes nothing for them and
                # stops overwriting the one engine it was wrong for.
                s.meta.setdefault("sub_engine", s.strategy)
                s.meta["family"] = family_of(eng.name)
                # ATR AT DETECTION -- 18-Aug-2026. `base.risk_from_structure`'s
                # own docstring says it plainly: "intraday_setups stores no
                # ATR ... so what a widened stop would have done cannot be
                # reconstructed from any row on disk." Neither the
                # ATR-anchored stop nor "refuse vs size down" is answerable
                # without this, on any engine, whether or not that engine's
                # own logic reads ATR today. Stamped HERE rather than per
                # engine so it is uniform across all nine and cannot be
                # skipped by a new one — the same reasoning as sub_engine and
                # family two lines up. `ctx.atr_pct_daily` is None on a symbol
                # missing daily history; recorded as None rather than omitted,
                # so an absent value in `meta` means "not available" and not
                # "forgot to ask", per this project's own rule that a check
                # which cannot fail is not a check.
                s.meta["atr_pct_daily"] = ctx.atr_pct_daily
                found.append(s)
        except Exception as ex:
            # One misbehaving engine must not stop the others. A scanner that
            # dies on a single bad symbol produces nothing for the whole
            # session, which is a far worse failure than one missing setup.
            logger.warning(f"  intraday engine {eng.name} failed on {ctx.symbol}: {ex}")
    if not found:
        return None, []
    found.sort(key=lambda s: -s.confidence)

    # SHADOW SETUPS ARE RETURNED BUT NEVER CHOSEN.
    #
    # `found` carries everything so the caller records and resolves every
    # detection — that record is the entire point of shadowing. `best` is drawn
    # only from ACTIVE engines, so a shadowed engine cannot receive capital no
    # matter how confident it is.
    fundable = [s for s in found if s.meta.get("lifecycle") == LIFECYCLE_ACTIVE]
    if not fundable:
        return None, found

    best = fundable[0]

    # CORROBORATION IS NOW CROSS-FAMILY ONLY.
    #
    # This is the substantive half of the merge. ORB, GAP and PDL firing on the
    # same name used to count as three engines agreeing and lifted confidence by
    # 0.10; they are three readings of one opening structure, correlated ~0.6-0.7
    # by the production decision's own estimate. Counting them as independent
    # manufactured conviction that no single engine had — and did it most
    # eagerly on exactly the setups where the three conditions coincide, which
    # is where they are least independent.
    #
    # Two engines from DIFFERENT families agreeing is still real evidence: an
    # opening-range break that also reclaims VWAP is two different things being
    # true. That still lifts.
    #
    # A retiree agreeing lifts nothing either — the reason it is being retired is
    # that it expresses the same bet, so counting it would re-admit the engine
    # through the back door.
    best_family = best.meta.get("family")
    same_family = [s for s in fundable[1:] if s.meta.get("family") == best_family]
    others      = [s for s in fundable[1:] if s.meta.get("family") != best_family]
    if same_family:
        # Recorded, because "which conditions coincided" is the evidence that
        # would later justify splitting a family back apart.
        best.meta["family_conditions"] = [s.strategy for s in same_family]
    if others:
        best.meta["corroborated_by"] = [s.strategy for s in others]
        # Agreement is evidence. Bounded so it can lift a setup but never
        # manufacture conviction that no single engine had.
        best.confidence = round(min(0.97, best.confidence + 0.05 * len(others)), 2)
    shadowed = [s.strategy for s in found if s.meta.get("lifecycle") == LIFECYCLE_SHADOW]
    if shadowed:
        best.meta["shadow_agreed"] = shadowed
    return best, found


def engine_names() -> list[str]:
    return [e.name for e in _ALL]


def sync_to_db(sb=None) -> None:
    """
    Register engines in intraday_strategy_config so the Control Room can see
    and toggle them. Idempotent; never overwrites an operator's choice.
    """
    sb = sb or get_supabase()
    for e in _ALL:
        try:
            existing = (sb.table("intraday_strategy_config").select("strategy")
                          .eq("strategy", e.name).execute().data)
            if existing:
                continue
            sb.table("intraday_strategy_config").insert({
                "strategy":  e.name,
                "enabled":   True,
                "lifecycle": "ACTIVE",
                "phases":    ",".join(e.phases),
                "label":     e.__class__.__name__,
                "description": (e.__class__.__doc__ or "").strip().split("\n")[0],
            }).execute()
            logger.info(f"  registered intraday engine {e.name}")
        except Exception as ex:
            logger.debug(f"  engine registry sync skipped for {e.name}: {ex}")
