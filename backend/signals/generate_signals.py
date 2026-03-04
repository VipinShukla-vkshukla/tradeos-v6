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
    msl    = sb.table("master_shortlist").select("*").eq("date", today).execute().data
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

    return stock_map, msl, open_map, regime_obj, events, asm_set, fo_ban_set, industry_map


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
    """EAP overlay — checks event calendar for timing signals."""
    pre_days  = int(cfg_eap.get("pre_event_days", 2))
    for ev in events:
        if not ev.get("is_active"):
            continue
        affected = str(ev.get("affected_sectors", "")).lower()
        if sector and sector.lower() in affected or ev.get("event_category") == "GLOBAL":
            start_d = ev.get("start_date")
            if not start_d:
                continue
            try:
                from datetime import date as _date
                sd = _date.fromisoformat(start_d[:10])
                days = (sd - today_ist()).days
                if 0 <= days <= pre_days:
                    return "AVOID_ENTRY"
                if -pre_days <= days < 0:
                    return "PRIORITISE"
            except Exception:
                pass
    return "NO_CHANGE"


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

    stock_map, msl, open_map, regime, events, asm_set, fo_ban_set, industry_map = load_today_data()

    regime_name = regime.get("regime", "NEUTRAL")
    block_buys  = cfg_bool("block_buys_risk_off", False) and regime_name == "RISK OFF"

    # Build sector rank lookup
    sb = get_supabase()
    sectors = sb.table("sector_strength").select("sector,rank").eq("date", str(run_date)).execute().data
    sector_rank = {s["sector"]: s["rank"] for s in sectors if s.get("rank")}

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

        # Regime gate
        regime_warning = False
        if position_state == "BUY_CANDIDATE" and regime_name == "RISK OFF":
            regime_warning = True
            if block_buys:
                signal_type = "BUY_BLOCKED_REGIME"

        # EAP override
        if eap_action == "AVOID_ENTRY" and position_state == "BUY_CANDIDATE":
            signal_type = "AVOID_ENTRY_EVENT"
        
        # Industry strength context
        industry     = msl_row.get("industry", "")
        ind_ctx      = industry_map.get(industry, {})
        ind_rank     = ind_ctx.get("rank")
        ind_top5     = ind_ctx.get("top5_flag", False)
        ind_state    = ind_ctx.get("industry_state", "")
        ind_rsi_d    = ind_ctx.get("avg_rsi_daily")

        # Boost score for top-5 industry
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
            "in_scanner": False,  # Phase 1
            "eap_action": eap_action,
            "regime": regime_name,
            "regime_warning": regime_warning,
            "asm_flag": asm_flag,
            "fo_ban_flag": fo_ban_flag,
            "ai_conviction": None,     # Phase 1
            "ai_suggested_action": None,
            "ai_provider": None,
            "ai_fallback_used": False,
            "fii_flag": None,          # Phase 1
            "industry": industry,
            "industry_rank": ind_rank,
            "industry_top5": ind_top5,
            "industry_state": ind_state,
            "industry_avg_rsi": ind_rsi_d,
        }
        signals.append(sig)

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
