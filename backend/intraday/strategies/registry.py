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
from config import cfg, cfg_bool, get_supabase
from intraday.strategies.base import Setup, SymbolContext
from intraday.strategies.orb import OpeningRangeBreakout
from intraday.strategies.vwap_reclaim import VwapReclaim
from intraday.strategies.gap_and_go import GapAndGo
from intraday.strategies.pullback import TrendPullback
from intraday.strategies.prev_day_levels import PrevDayLevelRetest
from intraday.strategies.squeeze import SqueezeExpansion
from intraday.strategies.range_fade import RangeFade

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
]


LIFECYCLE_ACTIVE  = "ACTIVE"     # evaluates, records, and may receive capital
LIFECYCLE_SHADOW  = "SHADOW"     # evaluates and records, but never receives capital
LIFECYCLE_RETIRED = "RETIRED"    # does not run at all


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
            if s:
                # Stamped at detection so every downstream consumer — the
                # recorder, the resolver, the weekly review — reads the same
                # answer to "was this fundable?". Deciding it later, from config
                # read at a different moment, is how a shadow engine's outcomes
                # end up scored as if they had been live.
                s.meta["lifecycle"] = engine_lifecycle(eng.name)
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
    # Corroboration counts only fundable agreement. A retiree agreeing with a
    # survivor is not independent evidence — the whole reason it is being
    # retired is that it expresses the same bet — so letting it lift confidence
    # would quietly re-admit the engine through the back door.
    others = [s for s in fundable[1:]]
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
