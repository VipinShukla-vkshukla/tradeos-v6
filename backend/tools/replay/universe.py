"""
The intraday universe as it stood on a PAST date. A copy, not an edit.

WHY THIS IS A COPY OF `scanner.build_universe`
-----------------------------------------------
`build_universe(sb, limit)` resolves its own date internally through
`_latest_date(sb)`, which returns the newest date carrying a non-null
`value_cr`. In production that is correct and deliberate — the bhavcopy for a
session is not published until after it closes, so "latest complete date" is the
only sane answer. In a replay it is fatal: every historical date would be
scanned against TODAY's cross-section.

The function takes no date parameter, so there is no injection point. The
options were to add one to `intraday/scanner.py` or to copy the body here. This
package copies, because the instruction for this stage is that production is not
modified, and because adding a replay-shaped parameter to a live scanner is how
a backtest starts changing what the system trades tonight.

**The filter arithmetic below is line-for-line `scanner.py:186-247`.** If that
function changes, this copy is stale and the drift is invisible. `tests/
test_replay_harness.py::test_universe_port_matches_production` guards exactly
that by running both against the same rows and requiring identical output.

WHAT IS DELIBERATELY DIFFERENT, AND WHY
----------------------------------------
1. **The date is supplied, never resolved.**
2. **The read is paged.** Production reads `.eq("date", d)` unpaged; that is
    ~500 rows today and safe, but this harness pages every read on principle —
    a silent truncation here would change the universe and therefore every
    downstream count.
3. **Null-`close` rows are dropped explicitly.** 2026-04-29 carries 2,476 rows
    against a 499–502 norm, 1,976 of them shells with `value_cr` but no price
    (REPLAY_DESIGN §8). Production's `close < min_price` check already discards
    them arithmetically (`float(None or 0)` is 0.0); this states it, counts it,
    and reports it, because a date whose candidate pool is five times normal
    would otherwise produce a silently different top-40.
4. **`skip_flagged` is forced OFF.** `safety_lists` has no history and cannot be
    given one (REPLAY_DESIGN §4.2) — ASM/GSM/F&O-ban state is a live snapshot,
    so applying TODAY's flags to a past date is lookahead. Running without them
    is the lesser error and its sign is known: flagged names are harder and more
    expensive to trade than the replay models, so results on them are
    OPTIMISTIC. Every report carries this label.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import get_supabase, fetch_all, cfg_int, cfg_float
from intraday.scanner import UniverseEntry


@dataclass
class UniverseResult:
    """The ranked universe for one date, plus the provenance to judge it."""
    date: str
    entries: list[UniverseEntry]
    rows_seen: int
    shells_dropped: int          # null-close rows — the 2026-04-29 signature
    rejected: dict[str, int]
    flags_applied: bool          # always False; see module docstring item 4

    @property
    def symbols(self) -> list[str]:
        return [e.symbol for e in self.entries]


def load_rows(day: str, sb=None) -> list[dict]:
    """
    One date's `stock_data_daily` rows, paged, with the columns the ranker needs.

    `order_by="symbol,date"` is not optional polish: this table has no `id`, so
    the default sort key raises 42703 on page one and the read returns nothing
    at all (Stage 2f, 2026-08-16).
    """
    sb = sb or get_supabase()
    return fetch_all(
        lambda: sb.table("stock_data_daily")
                  .select("symbol,close,value_cr,atr_pct,delivery_pct,volume,"
                          "avg_vol_20d,sector,industry,asm_flag,fo_ban_flag,"
                          "market_cap")
                  .eq("date", day),
        order_by="symbol,date")


def build_universe_at(day: str, sb=None, limit: int | None = None,
                      rows: list[dict] | None = None) -> UniverseResult:
    """
    The ranked intraday universe as of `day`. Point-in-time by construction.

    `rows` may be supplied to avoid a second read (and to let the port test feed
    both this and production the identical input).
    """
    limit = limit or cfg_int("intraday_max_universe", 40)
    rows = rows if rows is not None else load_rows(day, sb)
    if not rows:
        logger.warning(f"  replay universe: no rows for {day}")
        return UniverseResult(day, [], 0, 0, {}, False)

    min_price = cfg_float("intraday_min_price", 50.0)
    min_value = cfg_float("intraday_min_turnover_cr", 25.0)
    min_atr   = cfg_float("intraday_min_atr_pct", 1.20)
    max_atr   = cfg_float("intraday_max_atr_pct", 8.00)
    min_deliv = cfg_float("intraday_min_delivery_pct", 20.0)
    skip_flagged = False          # forced — see module docstring item 4

    out: list[UniverseEntry] = []
    rejected = {"price": 0, "liquidity": 0, "movement": 0,
                "delivery": 0, "flagged": 0}
    shells = 0

    for r in rows:
        sym = r.get("symbol")
        if r.get("close") is None:
            shells += 1
            rejected["price"] += 1
            continue
        close = float(r.get("close") or 0)
        value = float(r.get("value_cr") or 0)
        atr   = float(r.get("atr_pct") or 0)
        deliv = r.get("delivery_pct")
        deliv = float(deliv) if deliv is not None else None
        avg_vol = float(r.get("avg_vol_20d") or 0)

        if not sym or close < min_price:
            rejected["price"] += 1
            continue
        if skip_flagged and (r.get("asm_flag") or r.get("fo_ban_flag")):
            rejected["flagged"] += 1
            continue
        if value < min_value:
            rejected["liquidity"] += 1
            continue
        if atr < min_atr:
            rejected["movement"] += 1
            continue
        if atr > max_atr:
            rejected["movement"] += 1
            continue
        if deliv is not None and deliv < min_deliv:
            rejected["delivery"] += 1
            continue

        liq_score = min(1.0, value / max(min_value * 8, 1))
        mov_score = min(1.0, (atr - min_atr) / max(max_atr - min_atr, 0.01))
        score = round(0.55 * mov_score + 0.45 * liq_score, 4)

        out.append(UniverseEntry(
            symbol=sym, close=close, value_cr=round(value, 1),
            atr_pct=round(atr, 2), delivery_pct=deliv,
            sector=r.get("sector") or "", score=score,
            reason=f"ATR {atr:.2f}% · ₹{value:.0f}cr/day"
                   + (f" · {deliv:.0f}% delivery" if deliv is not None else ""),
            avg_vol_20d=avg_vol,
        ))

    out.sort(key=lambda e: -e.score)
    return UniverseResult(day, out[:limit], len(rows), shells, rejected, False)


def prev_day_reference(day: str, symbols: list[str], sb=None) -> dict[str, dict]:
    """
    Yesterday's close/high/low/atr/volume per symbol, as of `day`.

    STRICTLY BEFORE `day` — this is the field the whole harness turns on. The
    live path takes the newest row older than today (`engine.py:471-481`); doing
    it with `.lt("date", day)` rather than a fixed calendar offset is what makes
    it correct across weekends and holidays without consulting a calendar.
    """
    sb = sb or get_supabase()
    if not symbols:
        return {}
    rows = fetch_all(
        lambda: sb.table("stock_data_daily")
                  .select("symbol,date,high,low,close,atr_pct,volume,"
                          "value_cr,sector")
                  .in_("symbol", symbols)
                  .lt("date", day),
        order_by="symbol,date")

    latest: dict[str, dict] = {}
    for r in rows:                      # ascending by (symbol, date)
        latest[r["symbol"]] = r          # last write per symbol wins = newest
    return latest
