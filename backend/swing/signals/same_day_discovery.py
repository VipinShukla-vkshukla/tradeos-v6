"""
Same-day swing setup discovery — Phase 4 of the swing framework evolution
blueprint, 26-Aug-2026.

NOT a second decision system. It never calls decide(), never scores, never
places an order — it only proposes rows shaped like signal_output_daily,
for a candidate the evening pipeline did not list today, so a genuinely
new intraday-emerging setup does not have to wait until tomorrow evening
to be seen. A discovered row only ever competes for capital by later
flowing through evaluate_candidates()'s own decide()/ranking/allocator
chain (Stage 2, gated separately, off by default) — the exact same chain
every evening-sourced candidate already uses.

SCOPED TO THREE ENGINES ON PURPOSE — VBD, SBS, RSB. These are the only
three of the twelve evening screener engines whose trigger condition is
legitimately observable intraday from live price/volume plus yesterday's
already-computed stock_data_daily row. Excluded, and why:
  IAD / ACC   need post-close NSE delivery data.
  PEAD / EAP  event/results-calendar overlays, daily granularity.
  CTL/MOM/SEC trend-continuation signals better suited to the evening's
              full completed-day close than a partial live bar.

REUSES THE REAL SCREENER FUNCTIONS, UNMODIFIED — run_vbd/run_sbs/run_rsb
(swing/signals/screen_stocks.py) and compute_entry_zones/compute_trade_plan
(swing/compute/compute_msl.py) are called exactly as the evening pipeline
calls them, just against a one-symbol batch instead of the full universe.
No second copy of any trigger or sizing logic exists here.

ONE HONEST LIMITATION, NAMED RATHER THAN HIDDEN: VBD's own trigger gates
on delivery_pct >= 35, and NSE delivery data does not exist until after
close — there is nothing live to read. `_build_live_stock()` uses
YESTERDAY's delivery_pct as a same-day proxy rather than silently
inventing a number or skipping the gate; this is a deliberate
approximation, not a live figure, and is why Stage 1 ships shadow-only —
real forward-return evidence is what will show whether that proxy is good
enough to ever arm Stage 2.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from config import IST, cfg_bool

ENGINES = ("VBD", "SBS", "RSB")


def _simple_regime_ctx(regime: str | None) -> dict:
    """
    A deliberately minimal regime context — just the is_bear/is_bull/
    is_recovering flags compute_entry_zones() actually reads.

    swing.compute.compute_msl.build_regime_context() builds the FULL
    macro-overlay context (FII flow, global cues, macro data, news counts)
    the evening pipeline assembles once a day — reusing it here would mean
    fetching several evening-only inputs on a 300s cadence for a benefit
    (macro_overlay's regime_boost/vix_penalty) this narrow same-day module
    does not need. The swing regime label itself (market_regime table,
    already read once per cycle elsewhere in this codebase) is enough to
    answer the one question compute_entry_zones asks: is the required zone
    wider (bear/recovering) or tighter (bull) than neutral.
    """
    r = (regime or "NEUTRAL").upper()
    return {
        "is_bear": r in ("RISK OFF", "RISK_OFF", "BEAR"),
        "is_bull": r in ("RISK ON", "RISK_ON", "TRENDING", "BULL"),
        "is_recovering": r == "RECOVERING",
        "active_regime": r,
    }


def _build_live_stock(ctx, daily_row: dict) -> dict:
    """
    Merge yesterday's stock_data_daily row (every static field: sector,
    market_cap, rsi_weekly, atr_pct, bb_upper/lower, high_30d, rs_vs_nifty,
    ret_1m/3m, avg_vol_20d/50d, above_sma50, consol_range, and — see the
    module docstring — delivery_pct as a same-day proxy) with today's live
    price and volume from the ticker-fed SymbolContext.

    `ctx.volume_ratio` is intraday/strategies/base.py's own existing
    property — "today's volume so far against the 20-day average,
    time-adjusted" — reused unmodified rather than re-derived here.
    """
    s = dict(daily_row)
    if ctx is not None and getattr(ctx, "ltp", None):
        s["close"] = ctx.ltp
        s["current_price"] = ctx.ltp
        prev_close = getattr(ctx, "prev_close", None) or daily_row.get("close")
        if prev_close:
            s["pct_change"] = round((ctx.ltp - float(prev_close)) / float(prev_close) * 100, 2)
        vr = getattr(ctx, "volume_ratio", None)
        if vr is not None:
            s["vol_ratio"] = vr
    return s


def _trigger(s: dict, sector_rank: dict) -> str | None:
    """Which of the three engines, if any, fires for this single stock —
    calling the REAL screener functions with a one-symbol batch."""
    from swing.signals.screen_stocks import run_vbd, run_sbs, run_rsb

    sym = s.get("symbol")
    one = {sym: s}
    if run_vbd(one, sector_rank):
        return "VBD"
    if run_sbs(one, sector_rank, {}):
        return "SBS"
    if run_rsb(one, sector_rank):
        return "RSB"
    return None


def _latest_daily_rows(sb, symbols: list[str]) -> dict[str, dict]:
    """
    One symbol's most recent stock_data_daily row, for every symbol in
    `symbols` — client-side "keep the latest per symbol" over a bounded
    recent window, the same pattern this codebase already uses elsewhere
    for a per-symbol-latest read PostgREST cannot express directly.

    Explicitly bounded to the last 10 days — stock_data_daily is a table
    known to exceed PostgREST's 1000-row cap (55,963 rows measured
    15-Aug-2026), and this module only ever wants the single latest row
    per symbol, never history. Only "the last few sessions" is needed to
    find it, so this stays a small, genuinely bounded read rather than an
    unbounded one that happens not to have been truncated yet.
    """
    since = (datetime.now(IST).date() - timedelta(days=10)).isoformat()
    try:
        # paging-exempt: bounded on both axes — in_(symbols) is the
        # daemon's own watched-symbol set (a few dozen to ~150 names) AND
        # gte(date, since) caps it to 10 sessions; genuinely well under
        # the 1000-row cap, not merely assumed to be.
        rows = (sb.table("stock_data_daily").select("*")
                  .in_("symbol", symbols).gte("date", since)
                  .order("date", desc=True).execute().data or [])
    except Exception as e:
        logger.warning(f"  same_day_discovery: stock_data_daily read failed — {e}")
        return {}
    latest: dict[str, dict] = {}
    for r in rows:
        sym = r.get("symbol")
        if sym and sym not in latest:   # first hit per symbol is the latest, sorted desc
            latest[sym] = r
    return latest


def scan(symbols: list[str], contexts: dict, sb, trade_date: str) -> list[dict]:
    """
    One pass over `symbols` (the daemon's own watched-symbol set — no new
    data ingestion), on the same 300s slow-timer cadence as
    refresh_contexts()/gtt_manager.sync(). Returns the candidates newly
    written this call.
    """
    if not cfg_bool("swing_same_day_discovery_shadow", True):
        return []
    if not symbols:
        return []

    try:
        existing = {r["symbol"] for r in
                    sb.table("signal_output_daily").select("symbol")
                      .eq("date", trade_date).execute().data or []}
    except Exception as e:
        logger.warning(f"  same_day_discovery: could not read today's evening list — {e}")
        return []

    to_check = [s for s in symbols if s not in existing]
    if not to_check:
        return []

    try:
        already_discovered = {r["symbol"] for r in
                              sb.table("swing_same_day_candidates").select("symbol")
                                .eq("date", trade_date).execute().data or []}
    except Exception:
        already_discovered = set()
    to_check = [s for s in to_check if s not in already_discovered]
    if not to_check:
        return []

    try:
        regime_row = (sb.table("market_regime").select("regime")
                        .eq("date", trade_date).limit(1).execute().data or [])
        regime_ctx = _simple_regime_ctx(regime_row[0].get("regime") if regime_row else None)
    except Exception:
        regime_ctx = _simple_regime_ctx(None)

    try:
        sector_rank = {r["sector"]: r["rank"] for r in
                       sb.table("sector_strength").select("sector,rank")
                         .eq("date", trade_date).execute().data or []}
    except Exception:
        sector_rank = {}

    daily_rows = _latest_daily_rows(sb, to_check)

    from swing.compute.compute_msl import compute_entry_zones, compute_trade_plan

    discovered = []
    for sym in to_check:
        daily_row = daily_rows.get(sym)
        ctx = contexts.get(sym)
        if not daily_row or not ctx or not getattr(ctx, "ltp", None):
            continue

        s = _build_live_stock(ctx, daily_row)
        s["symbol"] = sym
        triggered_by = _trigger(s, sector_rank)
        if not triggered_by:
            continue

        try:
            ez_low, ez_high, _hot = compute_entry_zones(s, triggered_by, regime_ctx)
        except Exception as e:
            logger.debug(f"  same_day_discovery: {sym} zone computation failed — {e}")
            continue
        if not ez_low or not ez_high:
            continue
        try:
            plan = compute_trade_plan(s, ez_low, ez_high, regime_ctx)
        except Exception as e:
            logger.debug(f"  same_day_discovery: {sym} trade plan failed — {e}")
            continue
        if not plan or not plan.get("planned_stop") or not plan.get("planned_target"):
            continue

        discovered.append({
            "date": trade_date, "symbol": sym, "strategy": triggered_by,
            "entry_zone_low": ez_low, "entry_zone_high": ez_high,
            "planned_entry": plan.get("planned_entry"),
            "planned_stop": plan.get("planned_stop"),
            "planned_target": plan.get("planned_target"),
            "planned_risk_pct": plan.get("planned_risk_pct"),
            "expected_r": plan.get("expected_r"),
            "sector": s.get("sector"), "market_cap": s.get("market_cap"),
            "live_price_at_discovery": s.get("close"),
            "discovered_at": datetime.now(IST).isoformat(),
        })

    if discovered:
        try:
            sb.table("swing_same_day_candidates").upsert(
                discovered, on_conflict="date,symbol").execute()
            names = ", ".join(f"{r['symbol']}({r['strategy']})" for r in discovered)
            logger.info(f"  same_day_discovery: {len(discovered)} new candidate(s) — {names}")
        except Exception as e:
            logger.warning(f"  same_day_discovery: write failed — {e}")
            return []
    return discovered
