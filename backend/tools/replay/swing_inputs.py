"""
`screen_stocks.load_data`, made point-in-time. A copy — production is untouched.

THE DEFECT THIS EXISTS TO ROUTE AROUND
---------------------------------------
`swing/signals/screen_stocks.py::load_data(sb, today)` assembles the screener's
inputs, and **five of its nine reads are not point-in-time**. This is NOT a
production defect: in production `today` really is today, and every one of these
reads is correct. It does mean the function is unusable in a replay exactly as
written, and — worth recording — that any FUTURE tool calling it with a
historical date silently gets today's market state and no error.

    read                 production            here
    ------------------------------------------------------------------
    stock_data_daily     .eq("date", today)    same, paged
    sector_strength      .eq("date", today)    same, paged
    market_regime        order desc limit 1    .eq("date", D), else latest < D
    safety_lists         no date filter        EMPTY — see below
    event_calendar       .eq("is_active")      by event date window
    fii_dii_flow         order desc limit 1    .eq("date", D), else latest < D
    master_shortlist     .eq("date", today)    EMPTY — operator override
    open positions       today's live book     EMPTY — a replay owns its book

`safety_lists` IS UNRECONSTRUCTIBLE, AND THAT IS NOT A CHOICE
---------------------------------------------------------------
The table is keyed `PRIMARY KEY (symbol, list_type)` with **no date column**,
and its ingest deletes and rewrites the list on every run
(`ingest_asm_gsm.py:323`). ASM / GSM / F&O-ban state is a live snapshot with no
history anywhere in this database.

Applying TODAY's flags to a past date is lookahead — it would remove names that
were perfectly tradeable then because they are restricted now. Applying none is
the lesser error and its sign is stateable: **flagged names are harder and more
expensive to trade than the replay models, so any replayed result on an affected
name is OPTIMISTIC.** Every report carries that label.

NSE publishes historical ASM and F&O ban lists. Ingesting them with a date
column is separate work and is out of scope — recorded so it is not
rediscovered.

THE OPERATOR-OVERRIDE OMISSION IS ALSO DELIBERATE
--------------------------------------------------
`force_include` is empty. The `ingest_sheets` merge in production
(`screen_stocks.py:298-303`) folds an operator's manual picks into the
screener's input. Including them would credit the SYSTEM for a HUMAN's
selections, which is the one thing a backtest of the system must not do.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import get_supabase, fetch_all


@dataclass
class SwingInputs:
    """The screener's input bundle, as of one past date."""
    day: str
    stock_map: dict = field(default_factory=dict)
    sector_rank: dict = field(default_factory=dict)
    regime: dict = field(default_factory=dict)
    fii: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    asm_set: set = field(default_factory=set)       # always empty — see docstring
    fo_ban_set: set = field(default_factory=set)    # always empty — see docstring
    force_include: set = field(default_factory=set) # always empty — see docstring
    held_symbols: set = field(default_factory=set)  # always empty — replay owns its book
    sector_rank_date: str | None = None
    regime_date: str | None = None
    fii_date: str | None = None

    @property
    def caveats(self) -> list[str]:
        """Printed on every report built from these inputs. Not optional."""
        return [
            "safety_lists has no history: ASM/GSM/F&O-ban NOT applied — results "
            "on flagged names are OPTIMISTIC",
            "force_include empty: operator overrides excluded, so the system is "
            "credited only for its own picks",
            "held-symbol suppression empty: the replay owns its own book",
        ]


def _latest_on_or_before(sb, table: str, day: str, cols: str,
                         order_key: str) -> tuple[dict, str | None]:
    """
    The newest row on or before `day`. Never the newest row overall.

    This is the whole point-in-time correction for `market_regime` and
    `fii_dii_flow`: production takes `order("date", desc=True).limit(1)`, which
    in a replay resolves to today's regime applied to a date months earlier.
    """
    exact = fetch_all(lambda: sb.table(table).select(cols).eq("date", day),
                      order_by=order_key)
    if exact:
        return exact[0], day

    rows = (sb.table(table).select(cols)
              .lt("date", day).order("date", desc=True).limit(1)
              .execute().data or [])
    if rows:
        return rows[0], str(rows[0].get("date"))
    return {}, None


def load_data_at(day: str, sb=None) -> SwingInputs:
    """
    Every screener input as it stood on `day`. No read resolves to "now".

    Deliberately does NOT fall back to the latest available date when `day`
    itself has no rows — production does, and in production that is right. Here
    it would silently replay the wrong session.
    """
    sb = sb or get_supabase()
    inp = SwingInputs(day=day)

    stock_rows = fetch_all(
        lambda: sb.table("stock_data_daily").select("*").eq("date", day),
        order_by="symbol,date")
    if not stock_rows:
        logger.warning(f"  swing inputs: no stock_data_daily rows for {day} — "
                       f"NOT falling back to another date")
        return inp
    inp.stock_map = {r["symbol"]: r for r in stock_rows}

    # order_by="date,sector", NOT "sector". `sector_strength` has no `id`, and
    # `sector` alone is NOT UNIQUE — measured 25 distinct values across 1000
    # rows on the live table. A non-unique paging key is worse than a missing
    # one: a missing column raises 42703 on page one, while a non-unique key
    # pages silently and lets rows repeat and vanish across page boundaries.
    # Caught by tests/test_static_analysis.py before this ever ran.
    sector_rows = fetch_all(
        lambda: sb.table("sector_strength").select("sector,rank").eq("date", day),
        order_by="date,sector")
    inp.sector_rank = {r["sector"]: r["rank"] for r in sector_rows if r.get("rank")}
    inp.sector_rank_date = day if sector_rows else None
    if not inp.sector_rank:
        logger.warning(f"  swing inputs: sector_strength EMPTY for {day} — all "
                       f"nine engines gate on sector_rank and will return "
                       f"near-zero candidates")

    inp.regime, inp.regime_date = _latest_on_or_before(
        sb, "market_regime", day, "*", "date")
    inp.fii, inp.fii_date = _latest_on_or_before(
        sb, "fii_dii_flow", day, "fii_flag,fii_net_20d,fii_net_5d", "date")

    # event_calendar by the event's own date window rather than `is_active`,
    # which is a CURRENT flag and would import today's event set into history.
    #
    # The columns are `start_date` and `end_date` — there is NO `event_date`.
    # The first version of this filtered on one and would have raised 42703 on
    # page one; the static check caught it before it ran. An event counts as
    # live on `day` when it has started and has not yet ended.
    try:
        inp.events = fetch_all(
            lambda: sb.table("event_calendar").select("*")
                      .lte("start_date", day).gte("end_date", day),
            order_by="id")
    except Exception as e:
        logger.warning(f"  swing inputs: event_calendar unavailable for {day} "
                       f"— proceeding with none: {e}")
        inp.events = []

    logger.info(
        f"  swing inputs {day}: {len(inp.stock_map)} stocks · "
        f"{len(inp.sector_rank)} sectors (as of {inp.sector_rank_date}) · "
        f"regime {inp.regime.get('regime', '?')} (as of {inp.regime_date}) · "
        f"fii as of {inp.fii_date} · {len(inp.events)} events · "
        f"NO safety flags")
    return inp
