"""
TradeOS v6 — Phase 1: Position Event Monitor
Runs at 8:35 AM IST via pipeline_morning.yml, after ingest_nse_events.
Scans ONLY open_positions symbols for upcoming corporate events.
Sends a pre-market Telegram brief so you can act before market opens.

Risk tiers:
    HIGH   — results/earnings within 3 days → 🔴 ERROR to data_anomalies
    MEDIUM — board meeting within 2 days     → 🟡 WARN to data_anomalies
    LOW    — AGM/dividend/other within 5 days → 🟢 Telegram only (no anomaly row)

Wire in pipeline_morning.yml (after kite_reconcile, before market opens):
    - name: Position Event Monitor
      run: python backend/ingestion/position_event_monitor.py
      continue-on-error: true

Reads:
    open_positions      — only held positions are scanned (not full MSL)
    nifty_upcoming_events — primary source (refreshed by ingest_nse_events at 6 PM)
    event_calendar      — secondary source (deduplicated by symbol+event_date)

Writes:
    data_anomalies      — HIGH (ERROR severity) and MEDIUM (WARN severity) events

Supports --days N flag for custom lookahead (default: 5 days).
"""
import sys
import os
import argparse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# Event type → risk tier mapping
HIGH_EVENT_TYPES   = {"Results", "Earnings", "Quarterly Results", "Annual Results", "Financial Results"}
MEDIUM_EVENT_TYPES = {"Board Meeting", "Board Meeting For Results", "EGM"}
LOW_EVENT_TYPES    = {"AGM", "Dividend", "Bonus", "Rights", "Split", "Buy Back", "Buyback"}

# Default lookahead windows per tier (days)
HIGH_DAYS   = 3
MEDIUM_DAYS = 2
LOW_DAYS    = 5


def fetch_open_positions(sb) -> list[dict]:
    """Fetch all currently open positions."""
    rows = sb.table("open_positions").select(
        "symbol,strategy,sector,entry_price,active_sl,company_name,pnl_pct"
    ).execute().data
    return rows or []


def fetch_upcoming_events(sb, symbols: list[str], today: date, lookahead_days: int) -> list[dict]:
    """
    Fetch upcoming events for held symbols from both event sources.
    Deduplicates by (symbol, event_date) across nifty_upcoming_events and event_calendar.
    """
    if not symbols:
        return []

    cutoff = (today + timedelta(days=lookahead_days)).isoformat()
    today_str = today.isoformat()

    # Primary source: nifty_upcoming_events (auto-refreshed by ingest_nse_events)
    primary = sb.table("nifty_upcoming_events").select(
        "symbol,purpose,event_date,sector"
    ).in_("symbol", symbols).gte("event_date", today_str).lte("event_date", cutoff).execute().data or []

    # Secondary source: event_calendar (manual + AI entries)
    secondary = sb.table("event_calendar").select(
        "symbol,event_type,event_date,detail,is_active"
    ).in_("symbol", symbols).eq("is_active", True) \
     .gte("event_date", today_str).lte("event_date", cutoff).execute().data or []

    # Merge and deduplicate by (symbol, event_date)
    seen = set()
    merged = []

    for e in primary:
        key = (e["symbol"], e["event_date"])
        if key not in seen:
            seen.add(key)
            merged.append({
                "symbol":     e["symbol"],
                "event_type": e.get("purpose", ""),
                "event_date": e["event_date"],
                "detail":     e.get("purpose", ""),
                "source":     "nifty_upcoming_events",
            })

    for e in secondary:
        key = (e["symbol"], e["event_date"])
        if key not in seen:
            seen.add(key)
            merged.append({
                "symbol":     e["symbol"],
                "event_type": e.get("event_type", ""),
                "event_date": e["event_date"],
                "detail":     e.get("detail", ""),
                "source":     "event_calendar",
            })

    return merged


def classify_event(event_type: str, event_date: date, today: date) -> str:
    """
    Classify event into HIGH / MEDIUM / LOW risk tier based on type and timing.
    Returns 'HIGH', 'MEDIUM', 'LOW', or 'IGNORE'.
    """
    days_away = (event_date - today).days
    etype = str(event_type or "").strip()

    # Check HIGH tier
    for h in HIGH_EVENT_TYPES:
        if h.lower() in etype.lower():
            return "HIGH" if days_away <= HIGH_DAYS else ("LOW" if days_away <= LOW_DAYS else "IGNORE")

    # Check MEDIUM tier
    for m in MEDIUM_EVENT_TYPES:
        if m.lower() in etype.lower():
            return "MEDIUM" if days_away <= MEDIUM_DAYS else ("LOW" if days_away <= LOW_DAYS else "IGNORE")

    # Check LOW tier (AGM, dividend, etc.)
    for lo in LOW_EVENT_TYPES:
        if lo.lower() in etype.lower():
            return "LOW" if days_away <= LOW_DAYS else "IGNORE"

    # Unknown type: treat as LOW if within window
    return "LOW" if days_away <= LOW_DAYS else "IGNORE"


def write_anomaly(sb, symbol: str, event_type: str, event_date: str,
                  tier: str, days_away: int, today: str) -> bool:
    """Write HIGH/MEDIUM events to data_anomalies."""
    severity    = "ERROR" if tier == "HIGH" else "WARN"
    check_name  = f"position_event_{tier.lower()}"
    message     = (
        f"{symbol}: {event_type} in {days_away} day(s) on {event_date}. "
        f"Review open position before market open."
    )
    try:
        sb.table("data_anomalies").insert({
            "date":       today,
            "check_name": check_name,
            "severity":   severity,
            "value":      f"{days_away}d",
            "message":    message,
            "affected":   f"open_positions:{symbol}",
        }).execute()
        return True
    except Exception as e:
        logger.warning(f"data_anomalies write failed for {symbol}: {e}")
        return False


def send_telegram_brief(alerts: list[dict]):
    """Send consolidated pre-market event brief via Telegram."""
    if not alerts:
        return
    try:
        from control.telegram_bot import send_message
        lines = ["<b>📅 Pre-Market Event Alert — Held Positions</b>\n"]
        for a in alerts:
            tier_icon = "🔴" if a["tier"] == "HIGH" else ("🟡" if a["tier"] == "MEDIUM" else "🟢")
            lines.append(
                f"{tier_icon} <b>{a['symbol']}</b> — {a['event_type']} "
                f"in {a['days_away']}d ({a['event_date']})"
            )
        lines.append("\nReview positions before market opens at 9:15 AM IST.")
        send_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Telegram event brief failed: {e}")


def main(lookahead_days: int = 5):
    if is_kill_switch_active():
        logger.warning("Kill switch active — position_event_monitor skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Position Event Monitor starting (lookahead={lookahead_days}d) "
                f"{'[DRY RUN]' if DRY_RUN else ''}")

    sb    = get_supabase()
    today = today_ist()
    today_str = today.isoformat()

    # 1. Fetch open positions
    positions = fetch_open_positions(sb)
    if not positions:
        logger.info("No open positions — position_event_monitor done")
        return {"status": "ok", "positions": 0, "alerts": 0}

    symbols = [p["symbol"] for p in positions]
    logger.info(f"Scanning {len(symbols)} held positions for events in next {lookahead_days} days")

    # 2. Fetch upcoming events for held symbols
    events = fetch_upcoming_events(sb, symbols, today, lookahead_days)
    logger.info(f"Found {len(events)} upcoming events for held positions")

    if not events:
        logger.info("No upcoming events for held positions — all clear")
        return {"status": "ok", "positions": len(symbols), "alerts": 0}

    # 3. Classify events and build alert list
    alerts     = []
    anomalies  = 0

    for event in events:
        try:
            event_date = date.fromisoformat(str(event["event_date"]))
        except (ValueError, TypeError):
            continue

        tier      = classify_event(event["event_type"], event_date, today)
        days_away = (event_date - today).days

        if tier == "IGNORE":
            continue

        alert_item = {
            "symbol":     event["symbol"],
            "event_type": event["event_type"],
            "event_date": str(event["event_date"]),
            "days_away":  days_away,
            "tier":       tier,
            "detail":     event.get("detail", ""),
        }
        alerts.append(alert_item)

        if tier in ("HIGH", "MEDIUM"):
            log_fn = logger.error if tier == "HIGH" else logger.warning
            log_fn(f"{tier}: {event['symbol']} — {event['event_type']} in {days_away}d")

            if not DRY_RUN:
                wrote = write_anomaly(sb, event["symbol"], event["event_type"],
                                      str(event["event_date"]), tier, days_away, today_str)
                if wrote:
                    anomalies += 1
        else:
            logger.info(f"LOW: {event['symbol']} — {event['event_type']} in {days_away}d")

    if DRY_RUN:
        logger.info(f"[DRY RUN] {len(alerts)} alerts found (HIGH/MEDIUM/LOW):")
        for a in alerts:
            logger.info(f"  {a['tier']} — {a['symbol']}: {a['event_type']} in {a['days_away']}d")
        return {"status": "dry_run", "alerts": len(alerts)}

    # 4. Send Telegram brief for all tiers (HIGH + MEDIUM + LOW)
    if alerts:
        send_telegram_brief(alerts)

    high_count   = sum(1 for a in alerts if a["tier"] == "HIGH")
    medium_count = sum(1 for a in alerts if a["tier"] == "MEDIUM")
    low_count    = sum(1 for a in alerts if a["tier"] == "LOW")

    logger.success(
        f"Position Event Monitor done: {high_count} HIGH, {medium_count} MEDIUM, "
        f"{low_count} LOW | {anomalies} anomaly rows written"
    )
    return {
        "status":   "ok",
        "positions": len(symbols),
        "events":    len(events),
        "high":      high_count,
        "medium":    medium_count,
        "low":       low_count,
        "anomalies": anomalies,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradeOS v6 — Position Event Monitor")
    parser.add_argument("--days",    type=int, default=5, help="Lookahead window in days (default: 5)")
    parser.add_argument("--dry-run", action="store_true",  help="Print alerts without writing to DB")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    result = main(lookahead_days=args.days)
    print(result)
