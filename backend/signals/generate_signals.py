"""
TradeOS v6 — Signal Generation Engine v2
=========================================
Changes vs v1 (required for ml_provider_v2 26-feature training):

  NEW: 4 market-context fields written to signal_log at signal time.
  These capture the market conditions that existed WHEN THE SIGNAL FIRED
  — essential for point-in-time correct ML training (no temporal leakage).

  Fields added to signal dict:
    india_vix          → market_regime.india_vix (VIX at signal time)
    nifty_5d_chg_pct   → market_regime.nifty_5d_chg_pct (Nifty momentum)
    above_200dma_pct   → market_regime.above_200dma_pct (market breadth)
    fii_net_20d_ctx    → fii_dii_flow.fii_net_20d (FII 20d trend)

  load_today_data() now also returns fii_net_20d from fii_dii_flow.

  SQL required: sql_signal_log_market_context.sql
    (adds india_vix, nifty_5d_chg_pct, above_200dma_pct, fii_net_20d_ctx
     to signal_log — run before deploying this file)

  All other logic is identical to v1. This is a drop-in replacement.
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


def load_today_data():
    sb    = get_supabase()
    today = str(today_ist())

    stocks = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stocks:
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            last_date = latest[0]["date"]
            stocks = sb.table("stock_data_daily").select("*").eq("date", last_date).execute().data
            logger.info(f"No stock_data_daily for {today} — using last trading day {last_date}")

    msl = sb.table("master_shortlist").select("*").eq("date", today).execute().data
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
    industry_map = {r["industry"]: r for r in industry_str}
    stock_map  = {s["symbol"]: s for s in stocks}
    open_map   = {p["symbol"]: p for p in open_p}
    regime_obj = regime[0] if regime else {}
    asm_set    = {r["symbol"] for r in safety if r["list_type"] in ("ASM", "GSM", "ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety if r["list_type"] == "FO_BAN"}

    # v2: fetch fii_flag AND fii_net_20d for market-context signal capture
    fii_rows = (
        sb.table("fii_dii_flow")
        .select("fii_flag,fii_net_20d")
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    fii_flag    = fii_rows[0].get("fii_flag", "NEUTRAL")  if fii_rows else "NEUTRAL"
    fii_net_20d = fii_rows[0].get("fii_net_20d")          if fii_rows else None

    return stock_map, msl, open_map, regime_obj, events, asm_set, fo_ban_set, industry_map, fii_flag, fii_net_20d


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
    candidates = set()
    for sym in ctl_candidates:
        s   = stocks.get(sym, {})
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
    from datetime import date as _date

    pre_days      = int(cfg_eap.get("pre_event_days", 2))
    HIGH_IMPACT   = {"RESULTS","EARNINGS","QUARTERLY_RESULTS","BOARD_MEETING","BOARD MEETING","FINANCIAL RESULTS"}
    MEDIUM_IMPACT = {"AGM","ANALYST_MEET","CONCALL","INVESTOR_MEET"}
    LOW_IMPACT    = {"DIVIDEND","BONUS","SPLIT","BUYBACK","RIGHTS"}

    def _classify(ev):
        raw = (str(ev.get("event_type") or "") + " " + str(ev.get("purpose") or "")).upper()
        if any(k in raw for k in HIGH_IMPACT):   return "HIGH"
        if any(k in raw for k in MEDIUM_IMPACT): return "MEDIUM"
        if any(k in raw for k in LOW_IMPACT):    return "LOW"
        return "MEDIUM"

    best_action = "NO_CHANGE"
    for ev in events:
        if not ev.get("is_active"):
            continue
        ev_symbol = ev.get("symbol", "")
        affected  = str(ev.get("affected_sectors", "")).lower()
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
            days = (sd - today_ist()).days
        except Exception:
            continue
        impact = _classify(ev)
        if impact == "HIGH":
            if 0 <= days <= pre_days:
                return "AVOID_ENTRY"
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


def is_buy_candidate(msl_row: dict, open_map: dict, threshold: float = None) -> tuple:
    sym = msl_row.get("symbol")
    if sym in open_map:
        return False, "already_in_position"
    ep = msl_row.get("entry_zone_low")
    cp = msl_row.get("current_price")
    if not ep or not cp or ep <= 0:
        return False, "missing_price_data"
    eh         = msl_row.get("entry_zone_high") or ep * 1.02
    lifecycle  = (msl_row.get("lifecycle") or "").upper()
    entry_timing_type = (msl_row.get("entry_timing_type") or "").upper()
    momentum_state    = (msl_row.get("momentum_state") or "").upper()
    momentum_phase    = (msl_row.get("momentum_phase") or "").upper()
    velocity_state    = (msl_row.get("velocity_state") or "").upper()
    struct_edge       = (msl_row.get("struct_edge") or "").upper()
    reentry_mode      = (msl_row.get("reentry_mode") or "").upper()
    if threshold is None:
        threshold = buy_candidate_threshold()
    chase_soft     = 0.03
    chase_hard     = 0.06
    dist_from_high = (cp - eh) / eh
    if lifecycle == "EXIT":
        return False, "exit_lifecycle_no_entry"
    if entry_timing_type == "EXTENDED" and reentry_mode != "ELIGIBLE":
        return False, "extended_timing_no_reentry"
    if cp < ep * (1 - threshold):
        return False, "below_zone_wait"
    if cp < ep:
        return True, "near_zone_low"
    if ep <= cp <= eh:
        return True, "in_zone"
    is_strong_momentum = (
        momentum_state == "HOT" and
        momentum_phase in ("EARLY", "EXPANSION") and
        velocity_state == "ACCELERATING"
    )
    is_decent_momentum = (
        momentum_state not in ("WEAK",) and
        momentum_phase not in ("EXTENDED", "FLAT") and
        velocity_state != "DECELERATING"
    )
    is_chasing_type = entry_timing_type == "CHASING"
    if dist_from_high <= chase_soft:
        if is_strong_momentum:
            return True, "slight_above_strong_momentum"
        if is_decent_momentum and not is_chasing_type:
            return True, "slight_above_acceptable"
        return False, "slight_above_weak_momentum"
    if dist_from_high <= chase_hard:
        if is_strong_momentum and struct_edge == "YES" and not is_chasing_type:
            return True, "moderate_above_strong_confirmation"
        return False, "moderate_above_insufficient_momentum"
    return False, "far_above_extended"


def generate(run_date: date | None = None) -> list:
    run_date  = run_date or today_ist()
    strat_cfg = get_strategy_config()

    # v2: load_today_data returns fii_net_20d as well
    stock_map, msl, open_map, regime, events, asm_set, fo_ban_set, industry_map, fii_flag, fii_net_20d = load_today_data()

    def _resolve_regime(regime_obj: dict) -> str:
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
                pred_dt = pred_dt.replace(tzinfo=__import__("datetime").timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() / 3600 <= 24:
                return predicted
        except Exception:
            pass
        return manual

    regime_name = _resolve_regime(regime)
    block_buys  = cfg_bool("block_buys_risk_off", False) and regime_name == "RISK OFF"

    # v2: extract market-context fields from regime_obj once (shared across all signals)
    india_vix_ctx       = regime.get("india_vix")
    nifty_5d_ctx        = regime.get("nifty_5d_chg_pct")
    above_200dma_ctx    = regime.get("above_200dma_pct")
    # fii_net_20d_ctx from fii_dii_flow (loaded in load_today_data)

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

        if sym in tpo_set:        strat_tag = "TPO"
        elif sym in ctl_set:      strat_tag = "CTL"
        elif sym in sbs_set:      strat_tag = "SBS"
        else:                     strat_tag = msl_row.get("strategy_source", "")

        if in_pos:
            position_state = "OPEN_POSITION"
            action = pos.get("action_required", "").upper()
            if "EXIT" in action or "SELL" in action: signal_type = "EXIT"
            elif "ADD" in action:                    signal_type = "ADD"
            else:                                    signal_type = "HOLD"
        elif is_buy_candidate(msl_row, open_map)[0]:
            position_state = "BUY_CANDIDATE"
            signal_type    = "BUY_CANDIDATE"
        else:
            position_state = "WATCHING"
            signal_type    = "WATCH"
            if not show_watch:
                continue

        asm_flag    = sym in asm_set
        fo_ban_flag = sym in fo_ban_set
        if asm_flag:
            signal_type = "BLOCKED_ASM"

        regime_warning = False
        if position_state == "BUY_CANDIDATE":
            if regime_name == "RISK OFF":
                regime_warning = True
                if block_buys:
                    signal_type = "BUY_BLOCKED_REGIME"
            elif regime_name == "CAUTION":
                regime_warning = True
                score = round(score * 0.85, 1)
                logger.debug(f"{sym}: CAUTION regime — score penalised 15% to {score}")

        if eap_action == "AVOID_ENTRY" and position_state == "BUY_CANDIDATE":
            signal_type = "AVOID_ENTRY_EVENT"

        industry  = stock_map.get(sym, {}).get("industry", "") or msl_row.get("industry", "")
        ind_ctx   = industry_map.get(industry, {})
        ind_rank  = ind_ctx.get("rank")
        ind_top5  = ind_ctx.get("top5_flag", False)
        ind_state = ind_ctx.get("industry_state", "")
        ind_rsi_d = ind_ctx.get("avg_rsi_daily")

        if cfg("industry_scoring_active") == "true":
            if ind_top5:         score = (score or 0) + 10
            if ind_state == "STRONG": score = (score or 0) + 5

        sig = {
            "date":            str(run_date),
            "symbol":          sym,
            "company_name":    msl_row.get("company_name"),
            "sector":          sector,
            "strategy":        strat_tag,
            "signal_type":     signal_type,
            "position_state":  position_state,
            "score":           score,
            "in_rule_engine":  in_engine,
            "in_scanner":      False,
            "eap_action":      eap_action,
            "regime":          regime_name,
            "regime_warning":  regime_warning,
            "asm_flag":        asm_flag,
            "fo_ban_flag":     fo_ban_flag,
            "fii_flag":        fii_flag,
            "industry":        industry,
            "industry_rank":   ind_rank,
            "industry_top5":   ind_top5,
            "industry_state":  ind_state,
            "industry_avg_rsi":ind_rsi_d,
            # G1: stock technicals at signal time
            "rsi_daily":       stock_map.get(sym, {}).get("rsi_daily"),
            "rsi_weekly":      stock_map.get(sym, {}).get("rsi_weekly"),
            "adx":             stock_map.get(sym, {}).get("adx"),
            "vol_ratio":       stock_map.get(sym, {}).get("vol_ratio"),
            "delivery_pct":    stock_map.get(sym, {}).get("delivery_pct"),
            "atr_pct":         stock_map.get(sym, {}).get("atr_pct"),
            "ret_6m":          stock_map.get(sym, {}).get("ret_6m"),
            "dist_sma50":      stock_map.get(sym, {}).get("dist_sma50"),
            "days_in_list":    msl_row.get("days_in_list"),
            # CC4: Phase 2 context
            "rsi_monthly":     stock_map.get(sym, {}).get("rsi_monthly"),
            "rs_vs_nifty":     stock_map.get(sym, {}).get("rs_vs_nifty"),
            "consol_range":    stock_map.get(sym, {}).get("consol_range"),
            "ret_1m":          stock_map.get(sym, {}).get("ret_1m"),
            "ret_3m":          stock_map.get(sym, {}).get("ret_3m"),
            "above_sma50":     stock_map.get(sym, {}).get("above_sma50"),
            "breakout_setup":  stock_map.get(sym, {}).get("breakout_setup"),
            "validity_score":  msl_row.get("validity_score"),
            "expected_r_msl":  msl_row.get("expected_r"),
            "trend_maturity":  msl_row.get("trend_maturity"),
            "velocity_state":  msl_row.get("velocity_state"),
            "momentum_phase":  msl_row.get("momentum_phase"),
            # AG2: point-in-time sector rank
            "sector_rank_at_entry": sector_rank.get(sector) if sector else None,
            # Phase 2 fields
            "signal_subtype":       None,
            "score_adjusted":       score,
            "sheet_conflict":       False,
            "sheet_conflict_type":  None,
            "days_to_trigger_est":  None,
            # ── v2 NEW: market-context at signal time (for ml_provider_v2 training) ──
            # These 4 fields require sql_signal_log_market_context.sql migration first.
            # They capture MARKET CONDITIONS when this signal fired — critical for
            # temporal-correct ML training (no leakage from future market state).
            "india_vix":          india_vix_ctx,        # VIX environment at signal time
            "nifty_5d_chg_pct":   nifty_5d_ctx,         # Nifty momentum at signal time
            "above_200dma_pct":   above_200dma_ctx,      # Market breadth at signal time
            "fii_net_20d_ctx":    fii_net_20d,           # FII 20d trend at signal time
        }
        signals.append(sig)

    signals = _apply_scanner_crossref(signals, sb, run_date)
    return signals


def _apply_scanner_crossref(signals: list, sb, run_date) -> list:
    try:
        scanner_rows = (
            sb.table("scanner_signals")
            .select("symbol,pattern_type")
            .eq("date", str(run_date))
            .execute()
            .data
        )
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
                sig["in_scanner"]      = True
                sig["scanner_patterns"] = ",".join(scanner_map[sym])
                if sig.get("in_rule_engine") and sig.get("signal_type") in entry_types:
                    current = sig.get("score_adjusted") or sig.get("score") or 0
                    sig["score_adjusted"] = round(float(current) + 5, 1)
                    updated += 1

        logger.info(f"Scanner cross-ref: {len(scanner_map)} hits | {updated} signals +5")
    except Exception as e:
        logger.warning(f"Scanner cross-ref failed (non-fatal): {e}")
    return signals


def save_signals(signals: list):
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(signals)} signals")
        return
    sb = get_supabase()
    if signals:
        sb.table("signal_log").upsert(signals, on_conflict="date,symbol").execute()
    logger.info(f"✓ {len(signals)} signals written to signal_log")


def main():
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting")
        return {}

    logger.info("=" * 60)
    logger.info("STEP: Generate Signals v2")
    logger.info("=" * 60)

    signals = generate()

    from collections import Counter
    types = Counter(s["signal_type"] for s in signals)
    logger.info(f"Signal breakdown: {dict(types)}")

    buy_candidates = [s for s in signals if s["signal_type"] == "BUY_CANDIDATE"]
    exits          = [s for s in signals if s["signal_type"] == "EXIT"]
    risk_off_warns = [s for s in signals if s["regime_warning"]]
    logger.info(f"  BUY CANDIDATES: {len(buy_candidates)}")
    logger.info(f"  EXIT signals:   {len(exits)}")
    logger.info(f"  REGIME WARNS:   {len(risk_off_warns)}")

    # Log the v2 market context captured today
    if signals:
        s0 = signals[0]
        logger.info(
            f"  Market context captured: VIX={s0.get('india_vix','?')} "
            f"Nifty5d={s0.get('nifty_5d_chg_pct','?')} "
            f"Breadth={s0.get('above_200dma_pct','?')}% "
            f"FII20d={s0.get('fii_net_20d_ctx','?')}"
        )

    save_signals(signals)
    return {"signals": len(signals), "buy_candidates": len(buy_candidates), "exits": len(exits)}


if __name__ == "__main__":
    main()