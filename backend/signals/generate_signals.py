"""
TradeOS v6 — Signal Generation Engine
CTL + SBS + TPO + EAP rule engine
Independent of Sheet signals — computes from Supabase data
3-state position model: WATCHING / BUY_CANDIDATE / OPEN_POSITION
"""
import sys, json
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, get_strategy_config, get_system_config,
    cfg, cfg_bool, cfg_int, cfg_float, today_ist,
    buy_candidate_threshold, get_max_positions, DRY_RUN,
    is_kill_switch_active
)


# ── Helpers ──────────────────────────────────────────────────
def load_today_data():
    sb    = get_supabase()
    today = str(today_ist())

    stocks = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stocks:
        # Weekend or holiday — use last available trading day
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            last_date = latest[0]["date"]
            stocks = sb.table("stock_data_daily").select("*").eq("date", last_date).execute().data
            logger.info(f"No stock_data_daily for {today} — using last trading day {last_date}")

    msl    = sb.table("master_shortlist").select("*").eq("date", today).execute().data
    # ── AG1 FIX: MSL weekend/holiday fallback ────────────────────────────────
    # stock_data_daily already has this pattern; MSL was missing it.
    # Without it: if ingest_sheets fails or runs on a holiday, msl=[] →
    # generate() loops over nothing → 0 signals → silent failure, no Telegram alert.
    if not msl:
        latest_msl = sb.table("master_shortlist").select("date").order("date", desc=True).limit(1).execute().data
        if latest_msl:
            last_msl_date = latest_msl[0]["date"]
            msl = sb.table("master_shortlist").select("*").eq("date", last_msl_date).execute().data
            logger.warning(f"No MSL for {today} — using last available date {last_msl_date} ({len(msl)} rows)")
    open_p = sb.table("open_positions").select("*").execute().data
    regime = sb.table("market_regime").select("*").eq("date", today).execute().data
    if not regime:
        regime = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data
    events = sb.table("event_calendar").select("*").eq("is_active", True).execute().data
    safety = sb.table("safety_lists").select("symbol,list_type").execute().data
    industry_str = sb.table("industry_strength").select("*").eq("date", str(today_ist())).execute().data
    industry_map = {r["industry"]: r for r in industry_str}  # keyed by industry name
    stock_map  = {s["symbol"]: s for s in stocks}
    open_map   = {p["symbol"]: p for p in open_p}
    regime_obj = regime[0] if regime else {}
    asm_set    = {r["symbol"] for r in safety if r["list_type"] in ("ASM","GSM","ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety if r["list_type"] == "FO_BAN"}

    fii_rows = (sb.table("fii_dii_flow")
                  .select("fii_flag")
                  .order("date", desc=True)
                  .limit(1).execute().data)
    fii_flag = fii_rows[0].get("fii_flag", "NEUTRAL") if fii_rows else "NEUTRAL"

    return stock_map, msl, open_map, regime_obj, events, asm_set, fo_ban_set, industry_map, fii_flag


# ── Strategy filters ─────────────────────────────────────────
def run_ctl(stocks: dict, sectors_by_name: dict, cfg_ctl: dict) -> set:
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_ctl.get("max_sector_rank", 4):
            continue
        if (s.get("rsi_monthly") or 0) < cfg_ctl.get("min_monthly_rsi", 58):
            continue
        if (s.get("rsi_weekly") or 0) < cfg_ctl.get("min_weekly_rsi", 58):
            continue
        if (s.get("ret_6m") or -999) < cfg_ctl.get("min_6m_return", 0):
            continue
        if (s.get("atr_pct") or 999) > cfg_ctl.get("max_atr_pct", 4):
            continue
        if (s.get("market_cap") or 0) < cfg_ctl.get("min_market_cap", 500):
            continue
        candidates.add(sym)
    return candidates


def run_sbs(stocks: dict, sectors_by_name: dict, cfg_sbs: dict) -> set:
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_sbs.get("max_sector_rank", 7):
            continue
        if (s.get("rsi_daily") or 0) < cfg_sbs.get("min_daily_rsi", 55):
            continue
        if (s.get("rsi_weekly") or 0) < cfg_sbs.get("min_weekly_rsi", 56):
            continue
        if (s.get("vol_ratio") or 0) < cfg_sbs.get("min_vol_ratio", 1.3):
            continue
        if (s.get("consol_range") or 999) > cfg_sbs.get("max_consol_pct", 12):
            continue
        if (s.get("atr_pct") or 999) > cfg_sbs.get("max_atr_pct", 5):
            continue
        if (s.get("market_cap") or 0) < cfg_sbs.get("min_market_cap", 300):
            continue
        candidates.add(sym)
    return candidates


def run_tpo(ctl_candidates: set, stocks: dict, cfg_tpo: dict) -> set:
    """TPO is a child of CTL — only works on CTL winners."""
    candidates = set()
    for sym in ctl_candidates:
        s = stocks.get(sym, {})
        rsi = s.get("rsi_daily") or 0
        if not (cfg_tpo.get("min_rsi", 42) <= rsi <= cfg_tpo.get("max_rsi", 55)):
            continue
        if abs(s.get("dist_sma50") or 999) > cfg_tpo.get("max_dist_sma50", 3):
            continue
        if (s.get("atr_pct") or 999) > cfg_tpo.get("max_atr_pct", 4):
            continue
        candidates.add(sym)
    return candidates


def get_eap_action(symbol: str, sector: str, events: list, cfg_eap: dict) -> str:
    """
    EAP overlay — checks event calendar for timing signals.

    PATCHED Fix #6: Event-type weighting.
      HIGH   (RESULTS/EARNINGS/BOARD_MEETING): AVOID_ENTRY pre + PRIORITISE post
      MEDIUM (AGM/CONCALL):                    AVOID_ENTRY pre only
      LOW    (DIVIDEND/BONUS/SPLIT):           PRIORITISE post only, never AVOID_ENTRY

    Also added: per-symbol event check (event_calendar.symbol field).
    Sector-level events still checked as before.
    """
    from datetime import date as _date

    pre_days = int(cfg_eap.get("pre_event_days", 2))

    HIGH_IMPACT   = {"RESULTS", "EARNINGS", "QUARTERLY_RESULTS", "BOARD_MEETING",
                     "BOARD MEETING", "FINANCIAL RESULTS"}
    MEDIUM_IMPACT = {"AGM", "ANALYST_MEET", "CONCALL", "INVESTOR_MEET"}
    LOW_IMPACT    = {"DIVIDEND", "BONUS", "SPLIT", "BUYBACK", "RIGHTS"}

    def _classify(ev: dict) -> str:
        raw = (str(ev.get("event_type") or "") + " " +
               str(ev.get("purpose") or "")).upper()
        if any(k in raw for k in HIGH_IMPACT):
            return "HIGH"
        if any(k in raw for k in MEDIUM_IMPACT):
            return "MEDIUM"
        if any(k in raw for k in LOW_IMPACT):
            return "LOW"
        return "MEDIUM"  # unknown events default to medium caution

    best_action = "NO_CHANGE"

    for ev in events:
        if not ev.get("is_active"):
            continue

        # Check relevance: per-symbol OR sector-level OR global
        ev_symbol   = ev.get("symbol", "")
        affected    = str(ev.get("affected_sectors", "")).lower()
        is_relevant = (
            (ev_symbol and ev_symbol == symbol) or
            (sector and sector.lower() in affected) or
            ev.get("event_category") == "GLOBAL"
        )
        if not is_relevant:
            continue

        start_d = ev.get("start_date") or ev.get("event_date")
        if not start_d:
            continue
        try:
            sd   = _date.fromisoformat(str(start_d)[:10])
            days = (sd - today_ist()).days  # positive = upcoming, negative = past
        except Exception:
            continue

        impact = _classify(ev)

        if impact == "HIGH":
            if 0 <= days <= pre_days:
                return "AVOID_ENTRY"        # immediate — stop checking
            if -pre_days <= days < 0:
                best_action = "PRIORITISE"
        elif impact == "MEDIUM":
            if 0 <= days <= pre_days:
                if best_action != "AVOID_ENTRY":
                    best_action = "AVOID_ENTRY"
        elif impact == "LOW":
            if -pre_days <= days < 0:
                if best_action == "NO_CHANGE":
                    best_action = "PRIORITISE"

    return best_action


def is_buy_candidate(msl_row: dict, open_map: dict) -> bool:
    """entry_price ≈ current_price means ready to buy but not yet bought."""
    if msl_row.get("symbol") in open_map:
        return False  # already in position
    threshold = buy_candidate_threshold()
    ep = msl_row.get("entry_zone_low")
    cp = msl_row.get("current_price")
    if not ep or not cp:
        return False
    eh = msl_row.get("entry_zone_high") or ep * 1.01
    # In zone
    if ep <= cp <= eh:
        return True
    # Very close to low
    if ep > 0 and abs(cp - ep) / ep <= threshold:
        return True
    return False


# ── Main signal generation ───────────────────────────────────
def generate(run_date: date | None = None) -> list[dict]:
    run_date = run_date or today_ist()
    strat_cfg = get_strategy_config()

    stock_map, msl, open_map, regime, events, asm_set, fo_ban_set, industry_map, fii_flag = load_today_data()


    def _resolve_regime(regime_obj: dict) -> str:
        """
        PATCHED Fix #4 Edit B: Prefer ml_regime_classifier predicted_regime when:
          - predicted_regime field exists and is non-null
          - regime_predicted_at is within the last 24 hours
        Falls back to manual 'regime' field otherwise.
        This bridges Phase 2 ML predictions into signal scoring without
        changing the existing 'regime' column.
        """
        from datetime import datetime, timezone
        manual    = regime_obj.get("regime", "NEUTRAL")
        predicted = regime_obj.get("predicted_regime")
        pred_at   = regime_obj.get("regime_predicted_at")
        if not predicted or not pred_at:
            return manual
        try:
            if isinstance(pred_at, str):
                pred_dt = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))
            else:
                pred_dt = pred_at
            if pred_dt.tzinfo is None:
                pred_dt = pred_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() / 3600 <= 24:
                return predicted
        except Exception:
            pass
        return manual
    # PATCHED Fix #4 Edit C: regime_name now resolved from ML prediction when fresh
    regime_name = _resolve_regime(regime)
    block_buys  = cfg_bool("block_buys_risk_off", False) and regime_name == "RISK OFF"

    # Build sector rank lookup
    sb = get_supabase()
    sectors = sb.table("sector_strength").select("sector,rank").eq("date", str(run_date)).execute().data
    sector_rank = {s["sector"]: s["rank"] for s in sectors if s.get("rank")}

    cfg_ctl = json.loads(strat_cfg["CTL"]) if isinstance(strat_cfg.get("CTL"), str) else strat_cfg.get("CTL", {})
    cfg_sbs = json.loads(strat_cfg["SBS"]) if isinstance(strat_cfg.get("SBS"), str) else strat_cfg.get("SBS", {})
    cfg_tpo = json.loads(strat_cfg["TPO"]) if isinstance(strat_cfg.get("TPO"), str) else strat_cfg.get("TPO", {})
    cfg_eap = json.loads(strat_cfg["EAP"]) if isinstance(strat_cfg.get("EAP"), str) else strat_cfg.get("EAP", {})

    ctl_set = run_ctl(stock_map, sector_rank, cfg_ctl)
    sbs_set = run_sbs(stock_map, sector_rank, cfg_sbs)
    tpo_set = run_tpo(ctl_set, stock_map, cfg_tpo)
    rule_engine_set = ctl_set | sbs_set | tpo_set

    min_score  = cfg_float("min_score_to_show", 50)
    show_watch = cfg_bool("show_watching_stocks", True)

    signals = []
    for msl_row in msl:
        sym   = msl_row.get("symbol")
        score = msl_row.get("final_score") or 0
        if score < min_score:
            continue

        sector     = msl_row.get("sector", "")
        in_pos     = sym in open_map
        pos        = open_map.get(sym, {})
        eap_action = get_eap_action(sym, sector, events, cfg_eap)
        in_engine  = sym in rule_engine_set

        # Determine strategy tag
        if sym in tpo_set:
            strat_tag = "TPO"
        elif sym in ctl_set:
            strat_tag = "CTL"
        elif sym in sbs_set:
            strat_tag = "SBS"
        else:
            strat_tag = msl_row.get("strategy_source", "")

        # Position state
        if in_pos:
            position_state = "OPEN_POSITION"
            # Derive signal from position sheet
            action = pos.get("action_required", "").upper()
            if "EXIT" in action or "SELL" in action:
                signal_type = "EXIT"
            elif "ADD" in action:
                signal_type = "ADD"
            else:
                signal_type = "HOLD"
        elif is_buy_candidate(msl_row, open_map):
            position_state = "BUY_CANDIDATE"
            signal_type    = "BUY_CANDIDATE"
        else:
            position_state = "WATCHING"
            signal_type    = "WATCH"
            if not show_watch:
                continue

        # Safety gates
        asm_flag    = sym in asm_set
        fo_ban_flag = sym in fo_ban_set
        if asm_flag:
            signal_type = "BLOCKED_ASM"

        # ── PATCHED Fix #4 Edit D: Regime gate (RISK OFF + CAUTION) ──────────────
        regime_warning = False
        if position_state == "BUY_CANDIDATE":
            if regime_name == "RISK OFF":
                regime_warning = True
                if block_buys:
                    signal_type = "BUY_BLOCKED_REGIME"
            elif regime_name == "CAUTION":
                # CAUTION: warn but don't block — penalise score 15%
                regime_warning = True
                score = round(score * 0.85, 1)
                logger.debug(f"{sym}: CAUTION regime — score penalised 15% to {score}")

        # EAP override
        if eap_action == "AVOID_ENTRY" and position_state == "BUY_CANDIDATE":
            signal_type = "AVOID_ENTRY_EVENT"
        
        # Industry strength context
        industry = stock_map.get(sym, {}).get("industry", "") or msl_row.get("industry", "")
        ind_ctx      = industry_map.get(industry, {})
        ind_rank     = ind_ctx.get("rank")
        ind_top5     = ind_ctx.get("top5_flag", False)
        ind_state    = ind_ctx.get("industry_state", "")
        ind_rsi_d    = ind_ctx.get("avg_rsi_daily")

        # Industry rank and state — two scoring dimensions among ~10 total
        # Gated until industry_strength table has 30+ days of history
        if cfg("industry_scoring_active") == "true":
            if ind_top5:
                score = (score or 0) + 10
            if ind_state == "STRONG":
                score = (score or 0) + 5
        sig = {
            "date": str(run_date),
            "symbol": sym,
            "company_name": msl_row.get("company_name"),
            "sector": sector,
            "strategy": strat_tag,
            "signal_type": signal_type,
            "position_state": position_state,
            "score": score,
            "in_rule_engine": in_engine,
            "in_scanner": False,
            "eap_action": eap_action,
            "regime": regime_name,
            "regime_warning": regime_warning,
            "asm_flag": asm_flag,
            "fo_ban_flag": fo_ban_flag,
            "fii_flag": fii_flag,
            "industry": industry,
            "industry_rank": ind_rank,
            "industry_top5": ind_top5,
            "industry_state": ind_state,
            "industry_avg_rsi": ind_rsi_d,
            # ── G1 FIX: ML feature columns ──────────────────────
            "rsi_daily":     stock_map.get(sym, {}).get("rsi_daily"),
            "rsi_weekly":    stock_map.get(sym, {}).get("rsi_weekly"),
            "adx":           stock_map.get(sym, {}).get("adx"),
            "vol_ratio":     stock_map.get(sym, {}).get("vol_ratio"),
            "delivery_pct":  stock_map.get(sym, {}).get("delivery_pct"),
            "atr_pct":       stock_map.get(sym, {}).get("atr_pct"),
            "ret_6m":        stock_map.get(sym, {}).get("ret_6m"),
            "dist_sma50":    stock_map.get(sym, {}).get("dist_sma50"),
            "days_in_list":  msl_row.get("days_in_list"),
            # ── CC4: Phase 2 context columns ──────────────────────
            "rsi_monthly":    stock_map.get(sym, {}).get("rsi_monthly"),
            "rs_vs_nifty":    stock_map.get(sym, {}).get("rs_vs_nifty"),
            "consol_range":   stock_map.get(sym, {}).get("consol_range"),
            "ret_1m":         stock_map.get(sym, {}).get("ret_1m"),
            "ret_3m":         stock_map.get(sym, {}).get("ret_3m"),
            "above_sma50":    stock_map.get(sym, {}).get("above_sma50"),
            "breakout_setup": stock_map.get(sym, {}).get("breakout_setup"),
            "validity_score":  msl_row.get("validity_score"),
            "expected_r_msl":  msl_row.get("expected_r"),
            "trend_maturity":  msl_row.get("trend_maturity"),
            "velocity_state":  msl_row.get("velocity_state"),
            "momentum_phase":  msl_row.get("momentum_phase"),
            # ── AG2 FIX: sector_rank_at_entry — point-in-time rank stored at signal time.
            # ML training was hardcoding 5.0; inference was using today's rank retroactively.
            # Now: the rank that existed when the signal fired is preserved for clean training.
            "sector_rank_at_entry": sector_rank.get(sector) if sector else None,
            # Phase 2 redesign placeholders
            "signal_subtype":      None,
            "score_adjusted":      score,
            "sheet_conflict":      False,
            "sheet_conflict_type": None,
            "days_to_trigger_est": None,
        }
        signals.append(sig)

    # ── AG4 FIX: scanner_signals cross-reference pass ────────────────────────
    signals = _apply_scanner_crossref(signals, sb, run_date)

    return signals


def _apply_scanner_crossref(signals: list, sb, run_date) -> list:
    """
    AG4 FIX: scanner_signals cross-reference.

    independent_scanner.py writes VOLUME_SURGE, RS_BREAKOUT, POST_CONSOL,
    MEAN_REVERSION, DELIVERY_SURGE to scanner_signals daily. Previously
    generate_signals never read it -- in_scanner was always False.

    Now: reads scanner_signals for today after the signals list is built.
    Sets in_scanner=True and scanner_patterns on matching symbols.
    Adds +5 to score_adjusted for entry signals where both rule engine AND
    scanner confirm (triple confirmation: MSL + rule engine + scanner).
    """
    try:
        scanner_rows = (sb.table("scanner_signals")
                          .select("symbol,pattern_type")
                          .eq("date", str(run_date))
                          .execute().data)
        if not scanner_rows:
            return signals

        scanner_map = {}
        for row in scanner_rows:
            sym = row.get("symbol") or ""
            pat = row.get("pattern_type") or ""
            if sym:
                scanner_map.setdefault(sym, []).append(pat)

        entry_types = {"BUY_CANDIDATE", "PRIME_SETUP", "STAGED_ENTRY", "REENTRY_SETUP"}
        updated = 0
        for sig in signals:
            sym = sig["symbol"]
            if sym in scanner_map:
                sig["in_scanner"] = True
                sig["scanner_patterns"] = ",".join(scanner_map[sym])
                if sig.get("in_rule_engine") and sig.get("signal_type") in entry_types:
                    current = sig.get("score_adjusted") or sig.get("score") or 0
                    sig["score_adjusted"] = round(float(current) + 5, 1)
                    updated += 1
                    logger.debug(
                        f"{sym}: scanner cross-ref +5 -> score_adjusted={sig['score_adjusted']} "
                        f"patterns={sig['scanner_patterns']}"
                    )

        logger.info(
            f"Scanner cross-reference: {len(scanner_map)} scanner hits | {updated} signals boosted +5"
        )
    except Exception as e:
        logger.warning(f"Scanner cross-reference failed (non-fatal): {e}")

    return signals


def save_signals(signals: list[dict]):
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(signals)} signals")
        return
    sb = get_supabase()
    if signals:
        sb.table("signal_log").upsert(signals, on_conflict="date,symbol").execute()
    logger.info(f"✓ {len(signals)} signals written to signal_log")


def main():
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting signal generation")
        return {}

    logger.info("=" * 60)
    logger.info("STEP 2: Generate Signals")
    logger.info("=" * 60)

    signals = generate()

    # Summary
    from collections import Counter
    types = Counter(s["signal_type"] for s in signals)
    logger.info(f"Signal breakdown: {dict(types)}")
    logger.info(f"Regime: {signals[0]['regime'] if signals else 'N/A'}")

    buy_candidates = [s for s in signals if s["signal_type"] == "BUY_CANDIDATE"]
    exits          = [s for s in signals if s["signal_type"] == "EXIT"]
    risk_off_warns = [s for s in signals if s["regime_warning"]]

    logger.info(f"  BUY CANDIDATES: {len(buy_candidates)}")
    logger.info(f"  EXIT signals:   {len(exits)}")
    logger.info(f"  RISK OFF warns: {len(risk_off_warns)}")

    save_signals(signals)
    return {"signals": len(signals), "buy_candidates": len(buy_candidates), "exits": len(exits)}


if __name__ == "__main__":
    main()