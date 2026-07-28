"""
Event awareness for intraday — what the mechanical engines cannot see.

THE PROBLEM
-----------
Seven engines read price and volume. None of them knows that a company reports
results in ninety minutes, that the stock is in an F&O ban, or that its sector
just took a regulatory hit. A squeeze forming ahead of an earnings release looks
identical to any other squeeze — right up to the moment the release turns it
into a gap through the stop.

Intraday is where this hurts most. A swing position has weeks to recover from an
event surprise; an intraday position has hours, a stop measured in fractions of
a percent, and a broker that squares it off at 15:20 regardless.

WHAT IT DOES AND DELIBERATELY DOES NOT DO
------------------------------------------
It BLOCKS and it WARNS. It never boosts. An event cannot make a mediocre setup
worth taking — the asymmetry is real: a binary outcome adds variance, and
variance is the one thing a 0.7% target with a 0.5% stop cannot absorb. So the
gate only ever subtracts.

Reads the tables the swing pipeline already maintains — nifty_upcoming_events,
event_calendar, and the ASM/ban flags on stock_data_daily. Building a separate
intraday news feed would be a second source of truth for facts already recorded,
and the two would drift.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_int, cfg_bool, today_ist


@dataclass(frozen=True)
class EventVerdict:
    allow: bool
    severity: str          # CLEAR | WARN | BLOCK
    reason: str


class NewsGate:
    """
    Loaded once per session and refreshed on the slow timer — event data changes
    daily, not per tick, and a per-symbol query in the 15-second loop would be
    forty round trips for facts that have not moved.
    """

    def __init__(self, sb=None):
        self.sb = sb or get_supabase()
        self._events: dict[str, list[dict]] = {}
        self._flags: dict[str, dict] = {}
        self._sector_bias: dict[str, str] = {}
        self.loaded = False

    def refresh(self, symbols: list[str] | None = None) -> int:
        today = today_ist()
        horizon = (today + timedelta(days=cfg_int("intraday_event_horizon_days", 2))).isoformat()
        self._events.clear()
        self._flags.clear()
        self._sector_bias.clear()

        try:
            q = (self.sb.table("nifty_upcoming_events")
                   .select("symbol,purpose,details,event_date,days_to_event")
                   .gte("event_date", today.isoformat()).lte("event_date", horizon))
            if symbols:
                q = q.in_("symbol", symbols)
            for r in (q.execute().data or []):
                self._events.setdefault(r["symbol"], []).append(r)
        except Exception as e:
            logger.debug(f"  news_gate: events unavailable — {e}")

        # ASM / F&O ban are per-symbol trading conditions, not news, but they
        # belong to the same question: is this name safe to trade today.
        try:
            latest = (self.sb.table("stock_data_daily").select("date")
                        .order("date", desc=True).limit(1).execute().data or [])
            if latest:
                q = (self.sb.table("stock_data_daily")
                       .select("symbol,asm_flag,fo_ban_flag,sector")
                       .eq("date", latest[0]["date"]))
                if symbols:
                    q = q.in_("symbol", symbols)
                for r in (q.execute().data or []):
                    self._flags[r["symbol"]] = r
        except Exception as e:
            logger.debug(f"  news_gate: flags unavailable — {e}")

        # Sector-level events reach us through the snapshot the evening pipeline
        # already writes, rather than a second lookup into event_calendar.
        try:
            latest = (self.sb.table("signal_output_daily").select("date")
                        .order("date", desc=True).limit(1).execute().data or [])
            if latest:
                for r in (self.sb.table("signal_output_daily")
                            .select("sector,sector_event_bias")
                            .eq("date", latest[0]["date"]).execute().data or []):
                    bias = r.get("sector_event_bias")
                    if bias and r.get("sector"):
                        self._sector_bias[r["sector"]] = bias
        except Exception as e:
            logger.debug(f"  news_gate: sector bias unavailable — {e}")

        self.loaded = True
        n = len(self._events)
        if n:
            logger.info(f"  news_gate: {n} symbol(s) with events inside "
                        f"{cfg_int('intraday_event_horizon_days', 2)} days")
        return n

    def check(self, symbol: str, sector: str = "") -> EventVerdict:
        """Verdict for one symbol. Cheap — pure dict lookups."""
        if not cfg_bool("intraday_news_gate_enabled", True):
            return EventVerdict(True, "CLEAR", "")

        f = self._flags.get(symbol) or {}
        if f.get("fo_ban_flag"):
            return EventVerdict(False, "BLOCK",
                                "in the F&O ban list — cash-market pricing is distorted "
                                "for exactly these names")
        if f.get("asm_flag"):
            return EventVerdict(False, "BLOCK",
                                "under ASM surveillance — wider spreads and margin "
                                "penalties make an intraday round trip uneconomic")

        evs = self._events.get(symbol) or []
        if evs:
            soonest = min(evs, key=lambda e: e.get("days_to_event") or 99)
            days = soonest.get("days_to_event")
            purpose = (soonest.get("purpose") or "event").strip()
            if days is not None and days <= 0:
                return EventVerdict(False, "BLOCK",
                                    f"{purpose} TODAY — a binary outcome adds variance a "
                                    f"sub-1% stop cannot absorb")
            if days is not None and days <= 1:
                return EventVerdict(False, "BLOCK",
                                    f"{purpose} tomorrow — positions are routinely repriced "
                                    f"on the day before, not only on the day")
            return EventVerdict(True, "WARN", f"{purpose} in {days} days")

        bias = (self._sector_bias.get(sector) or "").upper()
        if bias in ("NEGATIVE", "RISK_OFF", "BEARISH"):
            return EventVerdict(True, "WARN", f"sector event bias {bias}")

        return EventVerdict(True, "CLEAR", "")
