"""
TradeOS v6 — Google Sheets Ingestion
Reads all 15 Sheet tabs → Supabase
Handles exact row offsets per tab (headers are NOT always row 1)
"""
import sys, re, json, math
from pathlib import Path
from datetime import date, datetime
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from loguru import logger

from config import (
    get_supabase, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS,
    DEFAULT_STRATEGY_PARAMS, DRY_RUN, today_ist
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# ── Sheet tab definitions: name → (header_row, data_start_row) ─
# Row numbers are 1-indexed as in Google Sheets
SHEET_TABS = {
    "MASTER_SHORTLIST":         (11, 12),
    "SECTOR_STRENGTH":          (3, 4),
    "EVENT_CALENDAR":           (4, 5),
    "LESSONS_LEARNED":          (1, 2),
    "NSE_HOLIDAYS":             (1, 2),
}

# ── Google Sheets client ─────────────────────────────────────
def get_sheets_service():
    creds = Credentials.from_service_account_file(
        str(Path(__file__).parent.parent / GOOGLE_CREDENTIALS),
        scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def read_tab(service, tab_name: str, max_rows: int = 2000) -> list[list]:
    """Read raw values from a Sheet tab."""
    range_str = f"'{tab_name}'!A1:BZ{max_rows}"
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=range_str,
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()
    return result.get("values", [])

# ── Parsers ──────────────────────────────────────────────────
def _scalar(v):
    if isinstance(v, pd.Series):
        return v.iloc[0] if not v.empty else None
    return v

def parse_date(v) -> date | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            from datetime import timedelta
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=int(v))).date()
        except Exception:
            return None
    s = str(v).strip()
    m = re.match(r"(\d+)\w*\s+(\w+)'(\d+)", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} 20{m.group(3)}", "%d %b %Y").date()
        except Exception:
            pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.split(" ")[0]).date()
    except Exception:
        pass
    return None

def parse_bool(v) -> bool | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return True
    if s in ("N", "NO", "FALSE", "0"):
        return False
    return None

def parse_num(v) -> float | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return None

def safe_str(v) -> str | None:
    v = _scalar(v)
    if v is None or v == "":
        return None
    s = str(v).strip()
    return None if s == "" else s

def safe_str(v) -> str | None:
    if v is None or v == "":
        return None
    s = str(v).strip()
    return None if s == "" else s

def rows_to_df(raw: list[list], header_row: int, data_start: int) -> pd.DataFrame:
    """Convert raw Sheet values to DataFrame using exact row offsets (1-indexed)."""
    if not raw or len(raw) < header_row:
        return pd.DataFrame()
    headers = raw[header_row - 1]
    # Normalize header names
    clean_headers = []
    seen_headers = {}
    for h in headers:
        s = str(h).strip()
        s = re.sub(r"^[^\w]+", "", s)
        s = re.sub(r"\s+", "_", s).lower()
        s = re.sub(r"[^\w]", "", s)
        s = s or f"col_{len(clean_headers)}"
        # Deduplicate: if column name already seen, append _2, _3, etc.
        if s in seen_headers:
            seen_headers[s] += 1
            s = f"{s}_{seen_headers[s]}"
        else:
            seen_headers[s] = 1
        clean_headers.append(s)
    data = raw[data_start - 1:]
    # Pad short rows
    rows = []
    for row in data:
        padded = list(row) + [None] * (len(clean_headers) - len(row))
        rows.append(padded[:len(clean_headers)])
    return pd.DataFrame(rows, columns=clean_headers)


# ── Tab-specific ingestors ────────────────────────────────────
    
def ingest_master_shortlist(service, sb):
    logger.info("Ingesting MASTER_SHORTLIST...")
    raw = read_tab(service, "MASTER_SHORTLIST", 200)
    df  = rows_to_df(raw, 11, 12)
    if df.empty:
        logger.warning("MASTER_SHORTLIST empty")
        return 0

    today = today_ist()
    rows  = []
    for _, r in df.iterrows():
        sym = safe_str(r.get("symbol"))
        if not sym or sym.startswith("─") or sym.startswith("="):
            continue
        entry_zone_low  = parse_num(r.get("entry_zone_low"))
        current_price   = parse_num(r.get("current_price"))
        # Determine position state
        state = "WATCHING"
        if current_price and entry_zone_low:
            entry_zone_high = parse_num(r.get("entry_zone_high")) or entry_zone_low * 1.01
            threshold = 0.005  # 0.5%
            if abs(current_price - entry_zone_low) / max(entry_zone_low, 1) <= threshold:
                state = "BUY_CANDIDATE"
            elif entry_zone_low <= current_price <= entry_zone_high:
                state = "BUY_CANDIDATE"
        in_pos = parse_bool(r.get("in_position"))
        if in_pos:
            state = "OPEN_POSITION"

        rows.append({
            "date": str(today),
            "symbol": sym,
            "company_name": safe_str(r.get("company_name")),
            "sector": safe_str(r.get("sector")),
            "strategy_source": safe_str(r.get("strategy_source")),
            "current_price": current_price,
            "final_score": parse_num(r.get("strategy_final_score")),
            "days_in_list": int(parse_num(r.get("days_in_list_output_3")) or 0) or None,
            "rank_vel_3d": parse_num(r.get("rank_velocity_3d_2")),
            "score_vel_5d": parse_num(r.get("score_velocity_5d_0")),
            "momentum_state": safe_str(r.get("momentum_state")),
            "momentum_phase": safe_str(r.get("momentum_phase")),
            "velocity_state": safe_str(r.get("velocity_state")),
            "trend_maturity": safe_str(r.get("trend_maturity")),
            "fv_low": parse_num(r.get("fair_value_zone_low")),
            "fv_high": parse_num(r.get("fair_value_zone_high")),
            "price_location": safe_str(r.get("price_location")),
            "dist_fv_pct": parse_num(r.get("distance_from_fair_value_")),
            "struct_edge": safe_str(r.get("structural_edge")),
            "entry_timing_type": safe_str(r.get("entry_timing_type")),
            "entry_ready": parse_bool(r.get("entry_ready")),
            "in_position": in_pos,
            "reentry_mode": safe_str(r.get("reentry_mode")),
            "lifecycle": safe_str(r.get("trend_lifecycle_state")),
            "expected_r": parse_num(r.get("expected_r_multiple")),
            "validity_score": parse_num(r.get("entry_validity_score_0100")),
            "exec_eligibility": safe_str(r.get("execution_eligibility")),
            "entry_zone_low": entry_zone_low,
            "entry_zone_high": parse_num(r.get("entry_zone_high")),
            "entry_mode": safe_str(r.get("entry_mode_15_3")),
            "dist_entry_pct": parse_num(r.get("distance_from_entry_")),
            "entry_action": safe_str(r.get("entry_action")),
            "opp_type": safe_str(r.get("entry_opportunity_type")),
            "base_rank": int(parse_num(r.get("base_rank")) or 0) or None,
            "base_score": parse_num(r.get("base_score")),
            "event_bias": safe_str(r.get("event_bias")),
            "event_sectors": safe_str(r.get("event_affected_sectors")),
            "upcoming_news": safe_str(r.get("upcoming_news")),
            "is_ipo": parse_bool(r.get("ipo_stock")),
            "trade_allowed": parse_bool(r.get("trade_allowed")),
            "suggested": safe_str(r.get("suggested_action")),
            "pos_type": safe_str(r.get("position_type")),
            "compute_source": "ingest_sheets",
            "notes": safe_str(r.get("notes")),
            "position_state": state,
        })
    
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} MSL rows")
        return len(rows)

    for i in range(0, len(rows), 200):
        sb.table("master_shortlist").upsert(
            rows[i:i+200], on_conflict="date,symbol"
        ).execute()
    logger.info(f"✓ MASTER_SHORTLIST: {len(rows)} rows upserted")
    # Reconcile — remove symbols no longer in today's sheet
    sheet_symbols = {r["symbol"] for r in rows}
    existing = sb.table("master_shortlist").select("symbol").eq("date", str(today)).eq("compute_source", "ingest_sheets").execute()
    db_symbols = {r["symbol"] for r in (existing.data or [])}
    removed = db_symbols - sheet_symbols
    if removed:
        logger.info(f"Removing {len(removed)} symbols no longer in MSL: {removed}")
        sb.table("master_shortlist").delete()\
            .eq("date", str(today))\
            .eq("compute_source", "ingest_sheets")\
            .in_("symbol", list(removed))\
            .execute()
    else:
        logger.info("No removed MSL symbols to reconcile")

    return len(rows)



def ingest_open_positions(service, sb):
    logger.info("Ingesting OPEN_POSITIONS...")
    raw = read_tab(service, "OPEN_POSITIONS", 50)
    df  = rows_to_df(raw, 4, 5)
    if df.empty:
        logger.warning("OPEN_POSITIONS empty")
        return 0

    # Clear existing and reload (positions are a current snapshot)
    rows = []
    for _, r in df.iterrows():
        sym = safe_str(r.get("symbol"))
        if not sym or sym.startswith("─") or sym.startswith("=") or sym.startswith("#"):
            continue
        
        # ── 03.04.2026 - Additional filter to ignore stocks where Entry Price = Current Price ──────────────────────────────────────────────
        entry_price   = parse_num(r.get("entry_price"))
        current_price = parse_num(r.get("current_price"))
        if entry_price is None or current_price is None or entry_price == current_price:
            continue
        
        rows.append({
            "symbol": sym,
            "company_name": safe_str(r.get("company_name")),
            "sector": safe_str(r.get("sector")),
            "strategy": safe_str(r.get("strategy")),
            "entry_date": str(parse_date(r.get("entry_date"))) if parse_date(r.get("entry_date")) else None,
            "entry_price": entry_price, #parse_num(r.get("entry_price")), 03.04.2026 - Additional filter to ignore stocks where Entry Price = Current Price
            "proposed_qty": parse_num(r.get("proposed_qty")),
            "actual_qty": parse_num(r.get("actual_quantity")),
            "invested_value": parse_num(r.get("invested_value")),
            "current_price": current_price, #parse_num(r.get("current_price")), 03.04.2026 - Additional filter to ignore stocks where Entry Price = Current Price
            "current_value": parse_num(r.get("current_value")),
            "unrealized_pnl": parse_num(r.get("unrealized_pl")),
            "pnl_pct": parse_num(r.get("pl_")),
            "locked_profit": parse_num(r.get("locked_profit")),
            "lifecycle": safe_str(r.get("trend_lifecycle")),
            "sl_type": safe_str(r.get("stop_loss_type")),
            "initial_sl_atr": parse_num(r.get("initial_sl_atr")),
            "high_water_mark": parse_num(r.get("high_water_mark")),
            "active_sl": parse_num(r.get("active_sl")),
            "exit_signal": safe_str(r.get("exit_signal")),
            "action_required": safe_str(r.get("action_required")),
            "event_risk": safe_str(r.get("event_risk")),
            "upcoming_news": safe_str(r.get("upcoming_news")),
            "target_1": parse_num(r.get("target_1")),
            "target_2": parse_num(r.get("target_2")),
            "status": "ACTIVE",
            "synced_at": "now()",
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would sync {len(rows)} open positions")
        return len(rows)

    # Upsert snapshot (use normalized symbol as key)
    if rows:
        sb.table("open_positions").upsert(rows, on_conflict="symbol").execute()
        logger.info(f"✓ OPEN_POSITIONS: {len(rows)} rows upserted")
    else:
        logger.info("No rows to upsert")

    # --- place after your upsert and before returning ---
    def _norm(s):
        return s.strip().upper() if s and isinstance(s, str) else None

    # current snapshot symbols (normalized)
    current_symbols = {_norm(r["symbol"]) for r in rows if r.get("symbol")}

    # fetch existing open_positions symbols and statuses
    res = sb.table("open_positions").select("symbol,status").execute()
    existing_rows = res.data or []
    existing_map = {_norm(r.get("symbol")): r.get("status") for r in existing_rows if r.get("symbol")}

    # symbols present in DB but missing from sheet
    removed = [s for s in existing_map.keys() if s not in current_symbols]
    if removed:
        # fetch closed symbols that have an exit_date
        closed_res = sb.table("closed_positions").select("symbol,exit_date").execute()
        closed_symbols = {
            _norm(r.get("symbol"))
            for r in (closed_res.data or [])
            if r.get("symbol") and r.get("exit_date")
        }

        to_delete = [s for s in removed if s in closed_symbols]
        to_mark_inactive = [s for s in removed if s not in closed_symbols]

        if DRY_RUN:
            logger.info(f"[DRY RUN] Would delete {len(to_delete)} symbols: {to_delete}")
            logger.info(f"[DRY RUN] Would mark INACTIVE {len(to_mark_inactive)} symbols: {to_mark_inactive}")
        else:
            batch_size = 100
            # delete closed ones in batches
            if to_delete:
                logger.info(f"Deleting {len(to_delete)} symbol(s) (found in closed_positions): {', '.join(to_delete)}")
                for i in range(0, len(to_delete), batch_size):
                    batch = to_delete[i:i+batch_size]
                    sb.table("open_positions").delete().in_("symbol", batch).execute()
                    logger.info(f"  ✓ Deleted batch {i//batch_size + 1}: {', '.join(batch)}")

            # mark the rest INACTIVE
            if to_mark_inactive:
                logger.info(f"Marking {len(to_mark_inactive)} symbol(s) as INACTIVE (missing from sheet, not in closed_positions): {', '.join(to_mark_inactive)}")
                for i in range(0, len(to_mark_inactive), batch_size):
                    batch = to_mark_inactive[i:i+batch_size]
                    sb.table("open_positions").update({"status": "INACTIVE", "synced_at": "now()"}).in_("symbol", batch).execute()
                    logger.info(f"  ✓ Marked INACTIVE batch {i//batch_size + 1}: {', '.join(batch)}")

            logger.info(f"✓ Reconciliation complete — Deleted: {len(to_delete)}, Marked INACTIVE: {len(to_mark_inactive)}")
    else:
        logger.info("No removed symbols to reconcile")
    
def find_signal_date(sb, symbol: str, entry_date: str) -> str | None:
    """
    Auto-detect signal_date from signal_log.
    Looks for BUY_CANDIDATE/WATCH signal within 5 days before entry_date.
    """
    try:
        from datetime import date, timedelta
        entry    = date.fromisoformat(str(entry_date)[:10])
        lookback = (entry - timedelta(days=5)).isoformat()
        rows = (sb.table("signal_log")
                  .select("date")
                  .eq("symbol", symbol)
                  .in_("signal_type", ["BUY_CANDIDATE", "WATCH"])
                  .gte("date", lookback)
                  .lte("date", str(entry))
                  .order("date", desc=True)
                  .limit(1).execute().data)
        return rows[0]["date"] if rows else None
    except Exception:
        return None 

def ingest_closed_positions(service, sb):
    logger.info("Ingesting CLOSED_POSITIONS...")
    raw = read_tab(service, "CLOSED_POSITIONS", 200)
    df  = rows_to_df(raw, 5, 6)
    if df.empty:
        return 0

    rows = []
    for _, r in df.iterrows():
        sym = safe_str(r.get("symbol"))
        if not sym or sym.startswith("Total"):
            continue

        entry_date = str(parse_date(r.get("entry_date"))) if parse_date(r.get("entry_date")) else None  # ← extract first

        rows.append({
            "symbol":                  sym,
            "company_name":            safe_str(r.get("company_name")),
            "sector":                  safe_str(r.get("sector")),
            "strategy":                safe_str(r.get("strategy")),
            "entry_date":              entry_date,                                                        # ← use extracted var
            "signal_date":             find_signal_date(sb, sym, entry_date) if entry_date else None,    # ← add here
            "entry_price":             parse_num(r.get("entry_price")),
            "proposed_qty":            parse_num(r.get("proposed_quantity")),
            "actual_qty":              parse_num(r.get("actual_quantity")),
            "invested_value":          parse_num(r.get("invested_value")),
            "exit_date":               str(parse_date(r.get("exit_date"))) if parse_date(r.get("exit_date")) else None,
            "exit_price":              parse_num(r.get("exit_price")),
            "exit_value":              parse_num(r.get("exit_value")),
            "realized_pnl":            parse_num(r.get("realized_pl")),
            "pnl_pct":                 parse_num(r.get("pl_")),
            "high_water_mark":         parse_num(r.get("high_water_mark")),
            "max_favorable_excursion": parse_num(r.get("max_favorable_excursion_")),
            "lifecycle_at_entry":      safe_str(r.get("lifecycle_at_entry")),
            "entry_timing_type":       safe_str(r.get("entry_timing_type_at_entr")),
            "reentry_mode":            safe_str(r.get("reentry_mode_at_entry")),
            "expected_r_at_entry":     parse_num(r.get("expected_r_at_entry")),
            "exit_reason":             safe_str(r.get("exit_reason")),
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} closed positions")
        return len(rows)

    for i in range(0, len(rows), 200):
        sb.table("closed_positions").upsert(
            rows[i:i+200], on_conflict="symbol,entry_date,exit_date"
        ).execute()
    logger.info(f"✓ CLOSED_POSITIONS: {len(rows)} rows upserted")
    return len(rows)


def ingest_sector_strength(service, sb):
    logger.info("Ingesting SECTOR_STRENGTH...")
    raw = read_tab(service, "SECTOR_STRENGTH", 28)
    df  = rows_to_df(raw, 3, 4)
    if df.empty:
        return 0

    today = today_ist()
    rows  = []
    for _, r in df.iterrows():
        sec = safe_str(r.get("sector"))
        if not sec:
            continue
        rows.append({
            "date": str(today),
            "sector": sec,
            "stock_count": int(parse_num(r.get("stocks_count")) or 0),
            "avg_rsi_daily": parse_num(r.get("avg_rsi_daily")),
            "avg_rsi_weekly": parse_num(r.get("avg_rsi_weekly")),
            "avg_rsi_monthly": parse_num(r.get("avg_rsi_monthly")),
            "avg_ret_6m": parse_num(r.get("avg_6m_return")),
            "breadth_sma50": parse_num(r.get("stocks_above_50_dma_sector_bredth")),
            "rank": int(parse_num(r.get("rank")) or 0) or None,
            "top4_flag": (int(parse_num(r.get("rank")) or 99) <= 5),
            "sector_state": safe_str(r.get("sector_state")),
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} sector rows")
        return len(rows)

    sb.table("sector_strength").upsert(rows, on_conflict="date,sector").execute()
    logger.info(f"✓ SECTOR_STRENGTH: {len(rows)} sectors upserted")
    return len(rows)

def ingest_industry_strength(service, sb):
    logger.info("Ingesting INDUSTRY_STRENGTH...")
    raw = read_tab(service, "SECTOR_STRENGTH", 100)
    df  = rows_to_df(raw, 32, 33)   # header row 33, data from row 34
    if df.empty:
        return 0

    today = today_ist()
    rows  = []
    for _, r in df.iterrows():
        ind = safe_str(r.get("industry"))
        if not ind:
            continue
        
        rows.append({
            "date": str(today),
            "industry": ind,
            "stock_count": int(parse_num(r.get("stocks_count")) or 0),
            "avg_rsi_daily": parse_num(r.get("avg_rsi_daily")), 
            "avg_rsi_weekly": parse_num(r.get("avg_rsi_weekly")),
            "avg_rsi_monthly": parse_num(r.get("avg_rsi_monthly")),
            "avg_ret_6m": parse_num(r.get("avg_6m_return")),
            "breadth_sma50": parse_num(r.get("stocks_above_50_dma_sector_bredth")),
            "rank": int(parse_num(r.get("rank")) or 0) or None,
            "top5_flag": (int(parse_num(r.get("rank")) or 99) <= 5),         # col still named top_4 in sheet
            "industry_state": safe_str(r.get("industry_state")),
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} industry rows")
        return len(rows)

    sb.table("industry_strength").upsert(rows, on_conflict="date,industry").execute()
    logger.info(f"✓ INDUSTRY_STRENGTH: {len(rows)} industries upserted")
    return len(rows)

def compute_regime_score(
    breadth_pct:        float | None,
    india_vix:          float | None,
    nifty_vs_50dma_pct: float | None,
    weekly_rsi:         float | None,
) -> float:
    """
    Deterministic 0.0–1.0 score representing regime strength.
    Weights: breadth 40% | VIX 30% | price vs 50DMA 20% | RSI 10%
 
    Thresholds for regime label (used in ingest_market_regime):
        score >= 0.65  → RISK_ON
        score >= 0.35  → NEUTRAL
        score <  0.35  → RISK_OFF
 
    NOTE: regime *label* still comes from the Google Sheet (human-reviewed).
    regime_score is the numeric confidence behind that label — used by AI
    to distinguish "borderline RISK_OFF (0.32)" from "deep RISK_OFF (0.05)".
    """
    score = 0.0
 
    # Breadth — 40% weight (most important: is the market move broad?)
    if breadth_pct is not None:
        if breadth_pct > 60:    score += 0.40
        elif breadth_pct > 40:  score += 0.20
        # else 0
 
    # VIX — 30% weight (fear gauge)
    if india_vix is not None:
        if india_vix < 14:      score += 0.30
        elif india_vix < 20:    score += 0.15
        # else 0 (VIX >= 20 = elevated fear)
 
    # Price vs 50DMA — 20% weight
    if nifty_vs_50dma_pct is not None:
        if nifty_vs_50dma_pct > 2:      score += 0.20
        elif nifty_vs_50dma_pct > -2:   score += 0.10
        # else 0 (price well below 50DMA)
 
    # Weekly RSI — 10% weight
    if weekly_rsi is not None:
        if weekly_rsi > 60:     score += 0.10
        elif weekly_rsi > 45:   score += 0.05
        # else 0
 
    return round(score, 2)


def ingest_event_calendar(service, sb):
    logger.info("Ingesting EVENT_CALENDAR...")
    raw = read_tab(service, "EVENT_CALENDAR", 100)
    df  = rows_to_df(raw, 4, 5)
    if df.empty:
        return 0

    rows = []
    for _, r in df.iterrows():
        name = safe_str(r.get("event_name"))
        if not name:
            continue
        sd = parse_date(r.get("start_date"))
        rows.append({
            "event_category": safe_str(r.get("event_category")),
            "event_name": name,
            "event_type": safe_str(r.get("event_type")),
            "start_date": str(sd) if sd else None,
            "end_date": str(parse_date(r.get("end_date"))) if parse_date(r.get("end_date")) else None,
            "affected_sectors": safe_str(r.get("affected_sectors")),
            "event_bias": safe_str(r.get("event_bias")),
            "event_intensity": safe_str(r.get("event_intensity")),
            "strategy_impact": safe_str(r.get("strategy_impact")),
            "notes": safe_str(r.get("notes")),
            "event_status": safe_str(r.get("event_status")),
            "is_active": parse_bool(r.get("is_active")),
            "priority": int(parse_num(r.get("priority")) or 0) or None,
            "is_risk_on_accelerator": safe_str(r.get("is_this_a_riskon_ac")),
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} events")
        return len(rows)

    sb.table("event_calendar").upsert(rows, on_conflict="event_name,start_date").execute()
    logger.info(f"✓ EVENT_CALENDAR: {len(rows)} events upserted")
    return len(rows)


def ingest_lessons(service, sb):
    logger.info("Ingesting LESSONS_LEARNED...")
    raw = read_tab(service, "LESSONS_LEARNED", 50)
    
    # Determine expected width from the header row
    header_row = raw[0]  # adjust index to match your header row index
    num_cols = len(header_row)
    
    # Pad all rows to the same width
    raw = [row + [""] * (num_cols - len(row)) for row in raw]
    df  = rows_to_df(raw, 1, 2)
    if df.empty:
        return 0

    rows = []
    for _, r in df.iterrows():
        scenario = safe_str(r.get("scenario_type"))
        if not scenario:
            continue
        rows.append({
            "date": str(parse_date(r.get("date")) or today_ist()),
            "scenario_type": scenario,
            "trigger_event": safe_str(r.get("trigger_event")),
            "linked_event_type": safe_str(r.get("linked_event_type")),
            "impacted_sector": safe_str(r.get("impacted_sector")),
            "scenario_context": safe_str(r.get("scenario__context")),
            "what_expected": safe_str(r.get("what_i_expected")),
            "what_happened": safe_str(r.get("what_actually_happened")),
            "what_failed": safe_str(r.get("what_failed")),
            "root_cause": safe_str(r.get("root_cause")),
            "corrective_rule": safe_str(r.get("corrective_rule_for_future")),
            "source": "MANUAL",
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} lessons")
        return len(rows)

    # Upsert not ideal here (no unique key) — insert new only
    existing = sb.table("lessons").select("scenario_type,trigger_event").execute().data
    existing_set = {(r["scenario_type"], r["trigger_event"]) for r in existing}
    new_rows = [r for r in rows if (r["scenario_type"], r["trigger_event"]) not in existing_set]
    if new_rows:
        sb.table("lessons").insert(new_rows).execute()
    logger.info(f"✓ LESSONS_LEARNED: {len(new_rows)} new lessons added")
    return len(new_rows)


def ingest_nse_holidays(service, sb):
    logger.info("Ingesting NSE_HOLIDAYS...")
    raw = read_tab(service, "NSE_HOLIDAYS", 50)
    df  = rows_to_df(raw, 1, 2)
    if df.empty:
        return 0

    rows = []
    for _, r in df.iterrows():
        d = parse_date(r.get("date"))
        if not d:
            continue
        rows.append({"date": str(d), "occasion": safe_str(r.get("occasion"))})

    if not DRY_RUN and rows:
        sb.table("nse_holidays").upsert(rows, on_conflict="date").execute()
    logger.info(f"✓ NSE_HOLIDAYS: {len(rows)} holidays")
    return len(rows)


def ingest_strategy_config(service, sb):
    """Extract strategy parameters from each STRATEGY_* tab → strategy_config table."""
    logger.info("Ingesting strategy configs...")

    configs = {
        "CTL": DEFAULT_STRATEGY_PARAMS["CTL"].copy(),
        "SBS": DEFAULT_STRATEGY_PARAMS["SBS"].copy(),
        "TPO": DEFAULT_STRATEGY_PARAMS["TPO"].copy(),
        "EAP": DEFAULT_STRATEGY_PARAMS["EAP"].copy(),
    }

    for strat, tab in [("CTL","STRATEGY_CTL"),("SBS","STRATEGY_SBS"),
                        ("TPO","STRATEGY_TPO"),("EAP","STRATEGY_EAP")]:
        try:
            raw = read_tab(service, tab, 20)
            for row in raw:
                if len(row) >= 2:
                    k = str(row[0]).strip()
                    v = row[1]
                    # Map known parameter names
                    key_map = {
                        "Max Sector Rank":          "max_sector_rank",
                        "Max Allowed CTL Positions":"max_positions",
                        "Min Monthly RSI":           "min_monthly_rsi",
                        "Min Weekly RSI":            "min_weekly_rsi",
                        "Min 6M Return (%)":         "min_6m_return",
                        "Max ATR %":                 "max_atr_pct",
                        "Min Market Cap (Cr)":       "min_market_cap",
                        "Min Daily RSI":             "min_daily_rsi",
                        "Min Volume Ratio":          "min_vol_ratio",
                        "Max Consolidation %":       "max_consol_pct",
                        "Min Daily RSI":             "min_daily_rsi",
                        "Max Daily RSI":             "max_daily_rsi",
                        "Max Distance from SMA50 %":"max_dist_sma50",
                        "Max CTL Rank":              "max_ctl_rank",
                        "Earnings Buffer (Days)":    "pre_event_days",
                        "Pre-Event Aggression":      "pre_event_aggression",
                        "Post-Event Aggression":     "post_event_aggression",
                        "Risk-Off Penalty":          "risk_off_penalty",
                    }
                    if k in key_map:
                        n = parse_num(v)
                        if n is not None:
                            configs[strat][key_map[k]] = n
        except Exception as e:
            logger.warning(f"Could not read {tab}: {e}")

    rows = [
        {"strategy": s, "params": json.dumps(p), "enabled": True}
        for s, p in configs.items()
    ]

    if not DRY_RUN:
        sb.table("strategy_config").upsert(rows, on_conflict="strategy").execute()
    logger.info(f"✓ STRATEGY_CONFIG: {len(rows)} strategies loaded")
    return len(rows)


# ── Main ─────────────────────────────────────────────────────
def main():
    from config import is_kill_switch_active
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting ingestion")
        return {}

    logger.info("=" * 60)
    logger.info("STEP 1: Google Sheets Ingestion")
    logger.info("=" * 60)

    sb      = get_supabase()
    service = get_sheets_service()

    results = {}
    steps = [
        ("strategy_config",     ingest_strategy_config),
        ("master_shortlist",    ingest_master_shortlist),
        ("open_positions",      ingest_open_positions),
        ("closed_positions",    ingest_closed_positions),
        ("sector_strength",     ingest_sector_strength),
        ("industry_strength",   ingest_industry_strength),
        ("event_calendar",      ingest_event_calendar),
        ("lessons",             ingest_lessons),
        ("nse_holidays",        ingest_nse_holidays),
    ]

    for name, fn in steps:
        try:
            results[name] = fn(service, sb)
        except Exception as e:
            logger.error(f"✗ {name} failed: {e}")
            results[name] = 0

    total = sum(v for v in results.values() if v is not None)
    logger.info(f"\n✓ Ingestion complete: {total} total rows across {len(results)} tables")
    return results


if __name__ == "__main__":
    main()
