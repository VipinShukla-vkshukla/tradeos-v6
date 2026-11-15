"""
TradeOS v7 — Calendar Prep + Sector Strength + Capital Reconciliation

This module combines three pipeline steps:
1. Calendar prep — NSE holidays + event_calendar rollover (runs EARLY)
2. Sector + industry strength — computes sector_strength/industry_strength (runs LATE, after compute_indicators)
3. Per-framework capital reconciliation — compares configured capital vs invested

The original file was completely replaced. This version restores all three functions.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dateutil import parser as dateparser
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    get_supabase, CAPITAL_IS_FALLBACK, IST, DRY_RUN, today_ist,
    is_kill_switch_active, cfg, cfg_float
)

# ==============================================================================
# CAPITAL RECONCILIATION (v2: per-framework)
# ==============================================================================

SNAPSHOT_KEY = "capital_snapshot_v2"  # v2: per-framework snapshot


def capital_status(sb=None, persist: bool = True) -> dict:
    """
    Returns dict with configured, available, invested, gap, gap_pct, severity, message, as_of.

    Keys:
      configured: {swing: float, intraday: float} — from Supabase config
      available:  {swing: float, intraday: float} — configured minus invested
      invested:   {swing: float, intraday: float} — from open_positions table
      gap:        {swing: float, intraday: float} — configured minus invested
      gap_pct:    {swing: float, intraday: float} — gap as percentage
      severity:   "OK" | "WARN"  — always OK unless config keys missing
      message:    human-readable description
      as_of:      ISO timestamps
    """

    sb = sb or get_supabase()
    now = datetime.now(IST).isoformat()

    # Read framework capital from Supabase (single source of truth)
    swing_cfg_rows = sb.table("system_config").select("value").eq("key", "swing_total_capital").execute().data
    intraday_cfg_rows = sb.table("system_config").select("value").eq("key", "intraday_total_capital").execute().data

    swing_cfg = float(swing_cfg_rows[0]["value"]) if swing_cfg_rows else 0.0
    intraday_cfg = float(intraday_cfg_rows[0]["value"]) if intraday_cfg_rows else 0.0

    # Query open positions to deduct invested capital
    # open_positions table must have a 'framework' column (SWING/INTRADAY)
    try:
        swing_holds = sb.table("open_positions").select("value").eq("framework", "SWING").eq("status", "ACTIVE").execute().data or []
        intraday_holds = sb.table("open_positions").select("value").eq("framework", "INTRADAY").eq("status", "ACTIVE").execute().data or []
    except Exception:
        swing_holds = []
        intraday_holds = []

    # Sum invested (value field — could be notional, ltp*qty, etc.)
    swing_invested = sum(
        float(h.get("value") or h.get("notional") or h.get("ltp") or 0)
        for h in swing_holds
    )
    intraday_invested = sum(
        float(h.get("value") or h.get("notional") or h.get("ltp") or 0)
        for h in intraday_holds
    )

    # Available capital = configured minus invested
    swing_available = swing_cfg - swing_invested
    intraday_available = intraday_cfg - intraday_invested

    # Gaps
    gap_swings = swing_cfg - swing_invested
    gap_pct_swings = (gap_swings / swing_cfg * 100) if swing_cfg else 0.0

    gap_intraday = intraday_cfg - intraday_invested
    gap_pct_intraday = (gap_intraday / intraday_cfg * 100) if intraday_cfg else 0.0

    # Severity: always OK for framework capital (it's a config, not broker-dependent)
    if swing_cfg == 0.0 or intraday_cfg == 0.0:
        severity = "WARN"
        message = ("Configured framework capital not found in system_config. "
                   "Set swing_total_capital and/or intraday_total_capital from the dashboard.")
    else:
        severity = "OK"
        message = (f"Swing: ₹{swing_cfg:,.0f} invested ₹{swing_invested:,.0f} available ₹{swing_available:,.0f} | "
                   f"Intraday: ₹{intraday_cfg:,.0f} invested ₹{intraday_invested:,.0f} available ₹{intraday_available:,.0f}")

    out = {
        "configured": {
            "swing": round(swing_cfg, 2),
            "intraday": round(intraday_cfg, 2),
        },
        "available": {
            "swing": round(swing_available, 2),
            "intraday": round(intraday_available, 2),
        },
        "invested": {
            "swing": round(swing_invested, 2),
            "intraday": round(intraday_invested, 2),
        },
        "gap": {
            "swing": round(gap_swings, 2),
            "intraday": round(gap_intraday, 2),
        },
        "gap_pct": {
            "swing": round(gap_pct_swings, 2),
            "intraday": round(gap_pct_intraday, 2),
        },
        "severity": severity,
        "message": message,
        "as_of": now,
    }

    # Persist snapshot for dashboard
    if persist:
        try:
            snapshot_data = {k: v for k, v in out.items() if k != "as_of"}
            row = {"key": SNAPSHOT_KEY, "value": json.dumps(payload := snapshot_data),
                   "updated_at": now}
            existing = sb.table("system_config").select("key").eq("key", SNAPSHOT_KEY).execute().data
            if existing:
                sb.table("system_config").update(
                    {"value": payload["value"], "updated_at": row["updated_at"]}
                ).eq("key", SNAPSHOT_KEY).execute()
            else:
                sb.table("system_config").insert({
                    **row,
                    "description": "Per-framework capital — written by control/capital_check.py v2",
                }).execute()
        except Exception as e:
            logger.debug(f"  capital snapshot not persisted: {e}")

    return out


def log_capital_status(sb=None) -> dict:
    """Run the check and log it at the severity it deserves."""
    st = capital_status(sb)
    msg = (f"Swing: ₹{st['configured']['swing']:,.0f} avail ₹{st['available']['swing']:,.0f} invested ₹{st['invested']['swing']:,.0f} | "
           f"Intraday: ₹{st['configured']['intraday']:,.0f} avail ₹{st['available']['intraday']:,.0f} invested ₹{st['invested']['intraday']:,.0f}")
    if st["severity"] == "BLOCK":
        logger.error(msg)
    elif st["severity"] == "WARN":
        logger.warning(msg)
    elif st["severity"] == "OK":
        logger.info(msg)
    else:
        logger.debug(msg)
    return st


def main_capital():
    """Run the capital status check — idempotent, safe to import from pipeline."""
    from config import get_supabase
    sb = get_supabase()
    return capital_status(sb, persist=True)


# ==============================================================================
# CALENDAR PREP — NSE Holidays + Event Calendar Rollover (runs EARLY)
# ==============================================================================

NSE_HOMEPAGE_URL    = "https://www.nseindia.com"
NSE_HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"


def fetch_nse_holidays(sb=None, segment="CM"):
    """
    Pull NSE's trading holiday calendar live from nseindia.com — no
    Sheet involved. This is the same JSON endpoint NSE's own site uses
    for its holiday widget: unofficial, but the standard approach used
    across the algo-trading community (nsetools, NseIndiaApi, etc.).

    segment: 'CM' (Capital Market / equity cash trading — default, what
    you want for an equity bot) or 'FO' (Futures & Options). NSE
    occasionally has small segment-specific differences.
    """
    sb = sb or get_supabase()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        # NSE blocks bare API calls without a primed session — hitting
        # the homepage first sets the cookies the API checks for.
        session.get(NSE_HOMEPAGE_URL, timeout=10)
        resp = session.get(NSE_HOLIDAY_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if segment not in data:
            logger.error(
                f"✗ NSE holidays: segment '{segment}' not in response "
                f"(available: {list(data.keys())}) — NSE may have changed "
                f"their API shape, check manually"
            )
            return 0

        rows = []
        for entry in data[segment]:
            date_str = entry.get("tradingDate") or entry.get("date")
            occasion = (
                entry.get("description")
                or entry.get("holidayDescription")
                or "Market Holiday"
            )
            if not date_str:
                continue
            d = dateparser.parse(date_str, dayfirst=True).date()
            rows.append({"date": str(d), "occasion": occasion})

        if not rows:
            logger.warning("✗ NSE holidays: fetched 0 rows — check segment/response shape")
            return 0

        if not DRY_RUN:
            sb.table("nse_holidays").upsert(rows, on_conflict="date").execute()
        logger.info(f"✓ NSE_HOLIDAYS: {len(rows)} holidays synced live from nseindia.com")
        return len(rows)

    except Exception as e:
        # A network hiccup or NSE blocking the request shouldn't break
        # the rest of the daily pipeline — same try/except pattern as
        # every other step in main(). The manually-seeded baseline from
        # 003_nse_holidays_data_seed.sql stays in place either way.
        logger.error(f"✗ NSE holidays live fetch failed: {e}")
        return 0


def roll_event_calendar(sb=None):
    """Advance recurring event_calendar rows + recompute is_active/event_status
    for every row. Calls the same fn_rollover_event_calendar() Postgres
    function that pg_cron runs daily — calling it again here is a cheap,
    idempotent safety net. Runs FIRST so nothing downstream reads stale data.
    """
    sb = sb or get_supabase()
    try:
        rolled = sb.rpc("fn_rollover_event_calendar", {}).execute().data or []
        for row in rolled:
            logger.info(
                f"  ↻ rolled '{row['rolled_event_name']}': "
                f"{row['prior_start']} → {row['next_start']}"
            )
        logger.info(f"✓ event_calendar rollover: {len(rolled)} event(s) advanced")
        return len(rolled)
    except Exception as e:
        logger.error(f"✗ event_calendar rollover failed: {e}")
        return 0


# ==============================================================================
# SECTOR + INDUSTRY STRENGTH (runs LATE, after compute_indicators)
# ==============================================================================

def resolve_trading_day_for_strength(sb, max_lookback: int = 10) -> str:
    """
    Most recent date in stock_data_daily that actually carries computed
    indicator data. Probing on rsi_daily (rather than merely on the date
    existing) is deliberate: ingest_bhavcopy creates rows for today with only
    delivery columns, so a plain date probe would return a date whose
    indicators are still NULL.
    """
    holidays = {r["date"] for r in sb.table("nse_holidays").select("date").execute().data}
    candidate = today_ist()

    for _ in range(max_lookback):
        ds = str(candidate)
        if candidate.weekday() < 5 and ds not in holidays:
            probe = (sb.table("stock_data_daily")
                       .select("date")
                       .eq("date", ds)
                       .not_.is_("rsi_daily", "null")
                       .limit(1).execute().data)
            if probe:
                if ds != str(today_ist()):
                    logger.info(f"  sector strength date resolved: {today_ist()} → {ds}")
                return ds
        candidate -= timedelta(days=1)

    raise RuntimeError(
        f"No stock_data_daily date with computed indicators found within "
        f"{max_lookback} days of {today_ist()} — compute_indicators has not run."
    )


def step_compute_sector_strength(sb=None):
    """
    Compute sector_strength + industry_strength for the last completed
    trading day.

    MUST RUN AFTER compute_indicators. The SQL aggregates sector, market_cap,
    rsi_*, ret_1m, rs_vs_nifty and above_sma50 out of stock_data_daily — every
    one of which is written by compute_indicators. ingest_bhavcopy creates
    today's stock_data_daily rows earlier in the pipeline but populates only
    value_cr / delivery_pct / delivery_qty, so running before compute_indicators
    means `coalesce(market_cap,0) > 0` matches nothing and the function writes
    ZERO rows.

    That is exactly what happened in production: run_pipeline Phase 2 called
    this at step 06 while compute_indicators ran at step 10, so sector_strength
    silently stopped updating on 2026-07-02 while stock_data_daily stayed
    current. Every sector_rank gate downstream (CTL <=4, SBS <=7, RVS <=9,
    VBD/RSB/MOM <=10, IAD <=12) was reading three-week-old ranks, and
    generate_signals — which has no fallback — read an empty dict and silently
    disabled CTL/SBS/TPO entirely.

    Two guards now make a recurrence impossible to miss:
      1. The date is the resolved last trading day, not today_ist(). On a
         weekend, a holiday, or a pre-bhavcopy run, today_ist() names a date
         with no indicator data.
      2. The RPC returns per-scope row counts and we raise if either is zero.
         The old signature returned void, so the caller logged success
         unconditionally — which is why this went unnoticed for 22 days.
    """
    sb    = sb or get_supabase()
    today = resolve_trading_day_for_strength(sb)

    result = sb.rpc("fn_compute_sector_industry_strength", {
        "p_date":       today,
        "p_min_stocks": 5,
        "p_lookback":   5,
    }).execute()

    counts = {r["scope"]: int(r["rows_written"] or 0) for r in (result.data or [])}
    sectors, industries = counts.get("sector", 0), counts.get("industry", 0)

    if sectors == 0 or industries == 0:
        raise RuntimeError(
            f"sector/industry strength wrote {sectors} sectors and {industries} "
            f"industries for {today}. This means stock_data_daily has no rows "
            f"with sector + market_cap + indicators populated for that date — "
            f"compute_indicators has not run, or ran for a different date. "
            f"Signals generated after this point would use stale sector ranks."
        )

    logger.info(
        f"✓ sector/industry strength computed for {today}: "
        f"{sectors} sectors, {industries} industries"
    )
    return sectors


# ==============================================================================
# PIPELINE ENTRY POINTS
# ==============================================================================

# SPLIT INTO TWO PIPELINE STEPS (see run_pipeline.py Phase 2)
#
#   main()          — calendar prep. Holidays and event rollover. Depends on
#                     nothing, so it runs EARLY: every downstream step resolves
#                     trading days against nse_holidays, and the event rollover
#                     must land before anything reads event_calendar.
#
#   main_strength() — sector/industry strength. Depends on compute_indicators
#                     having populated stock_data_daily, so it runs LATE.
#
# These used to be one step running early, which meant sector strength was
# computed against a stock_data_daily that only ingest_bhavcopy had touched.
# See step_compute_sector_strength's docstring for the full failure mode.
# ─────────────────────────────────────────────────────────────


def main() -> dict:
    """Calendar prep — runs EARLY in the pipeline. No data dependencies."""
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting ingestion")
        return {}

    logger.info("=" * 60)
    logger.info("Calendar prep — NSE holidays + event_calendar rollover")
    logger.info("=" * 60)

    sb = get_supabase()
    results = {}

    # Holidays: live NSE API
    try:
        n = fetch_nse_holidays(sb)
        results["nse_holidays"] = n
    except Exception as e:
        logger.error(f"✗ nse_holidays failed: {e}")
        results["nse_holidays"] = 0

    try:
        results["event_calendar"] = roll_event_calendar(sb)
    except Exception as e:
        logger.error(f"✗ event_calendar failed: {e}")
        results["event_calendar"] = 0

    logger.info(f"✓ Calendar prep complete: {results}")
    return results


def main_strength() -> dict:
    """
    Sector + industry strength — runs LATE, after compute_indicators.

    Deliberately NOT wrapped in try/except. Every sector_rank gate in
    screen_stocks and generate_signals depends on this output, and
    generate_signals has no fallback at all — an empty sector_rank dict
    silently disables CTL/SBS/TPO rather than erroring. Failing loudly here
    is far better than producing a day of signals from stale ranks.
    """
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting sector strength")
        return {}

    logger.info("=" * 60)
    logger.info("Sector + industry strength")
    logger.info("=" * 60)
    sb = get_supabase()
    return {"sector_industry_strength": step_compute_sector_strength(sb)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeOS v7 — calendar prep / sector strength / capital check")
    ap.add_argument("--strength", action="store_true",
                    help="Run sector/industry strength only (requires compute_indicators to have run)")
    ap.add_argument("--capital", action="store_true",
                    help="Run capital reconciliation only")
    args = ap.parse_args()
    if args.capital:
        import pprint
        pprint.pprint(main_capital())
    elif args.strength:
        print(main_strength())
    else:
        print(main())