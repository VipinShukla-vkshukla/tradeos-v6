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
from config import cfg_bool, get_supabase
from intraday.strategies.base import Setup, SymbolContext
from intraday.strategies.orb import OpeningRangeBreakout
from intraday.strategies.vwap_reclaim import VwapReclaim

_ALL = [OpeningRangeBreakout(), VwapReclaim()]


def enabled_engines() -> list:
    """
    Engines switched on, read live so the Control Room can disable one
    mid-session without a restart.

    Defaults to ON for a registered engine: a new engine that silently does
    nothing because someone forgot a config row is worse than one that runs and
    can be turned off.
    """
    out = []
    for e in _ALL:
        if cfg_bool(f"intraday_engine_{e.name.lower()}_enabled", True):
            out.append(e)
    return out


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
                found.append(s)
        except Exception as ex:
            # One misbehaving engine must not stop the others. A scanner that
            # dies on a single bad symbol produces nothing for the whole
            # session, which is a far worse failure than one missing setup.
            logger.warning(f"  intraday engine {eng.name} failed on {ctx.symbol}: {ex}")
    if not found:
        return None, []
    found.sort(key=lambda s: -s.confidence)
    best = found[0]
    if len(found) > 1:
        best.meta["corroborated_by"] = [s.strategy for s in found[1:]]
        # Agreement is evidence. Bounded so it can lift a setup but never
        # manufacture conviction that no single engine had.
        best.confidence = round(min(0.97, best.confidence + 0.05 * (len(found) - 1)), 2)
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
