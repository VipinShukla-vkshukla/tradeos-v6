"""
TradeOS v6 — Phase 2+: Kite Position Reconciliation
=====================================================
NEW SCRIPT (Strategic Fix #2 — execution integrity).

Runs once daily at 8:45 AM IST (after kite_token_refresh.py, before market open)
via pipeline_morning.yml. Compares actual Zerodha holdings against open_positions
table in Supabase and flags any divergence.

Why this exists:
  execution_engine.py places orders and writes to open_positions. But Supabase
  and Kite can diverge silently when:
    - A Kite GTT fires and auto-exits a position
    - You manually close something in Zerodha app
    - A partial fill leaves fewer shares than expected
    - An order was rejected but open_positions was already written

  Without reconciliation, risk_manager uses stale position data → incorrect
  portfolio risk calculations → bad sizing decisions.

Reconciliation outcomes per symbol:
  MATCHED         — qty and symbol match exactly. No action.
  QTY_MISMATCH    — same symbol, different qty. Telegram alert + anomaly.
  KITE_ONLY       — in Kite but not in Supabase (unknown position). Critical alert.
  DB_ONLY         — in Supabase but not in Kite (position may be closed). Alert.

Tables read:   open_positions, system_config
Tables write:  open_positions (current_price, kite_qty, reconcile_status),
               data_anomalies
External:      Kite Connect API (kite.holdings() — CNC delivery positions)

Wire:  pipeline_morning.yml — runs at 8:45 AM IST after kite_token_refresh.py
Phase: 2+ (requires Kite token)
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, logger


def fetch_kite_holdings() -> list[dict]:
    """
    Fetch actual CNC delivery holdings from Kite.
    Returns list of {symbol, qty, avg_price, last_price}.
    Falls back to empty list if Kite unavailable.
    """
    try:
        from kite.kite_client import get_kite
        kite     = get_kite()
        holdings = kite.holdings()          # delivery (CNC) holdings
        result   = []
        for h in holdings:
            qty = int(h.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            result.append({
                "symbol":      h.get("tradingsymbol", ""),
                "qty":         qty,
                "avg_price":   float(h.get("average_price", 0) or 0),
                "last_price":  float(h.get("last_price", 0) or 0),
            })
        logger.info(f"Kite holdings fetched: {len(result)} positions")
        return result
    except Exception as e:
        logger.error(f"Kite holdings fetch failed: {e}")
        return []


def send_telegram(message: str):
    try:
        from alerts.send_alerts import send_telegram_message
        send_telegram_message(message)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def log_anomaly(sb, symbol: str, check_name: str, severity: str, value: str, message: str):
    try:
        sb.table("data_anomalies").insert({
            "date":       today_ist().isoformat(),
            "check_name": check_name,
            "severity":   severity,
            "value":      value,
            "message":    message,
            "affected":   f"open_positions:{symbol}",
            "created_at": datetime.now(IST).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"Could not log anomaly: {e}")


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — kite_reconcile skipped")
        return {"status": "skipped"}

    sb = get_supabase()

    # Load Supabase open_positions
    db_positions = sb.table("open_positions") \
        .select("symbol, actual_qty, entry_price, invested_value, strategy") \
        .execute().data
    db_map = {p["symbol"]: p for p in db_positions}

    # Load Kite holdings
    kite_holdings = fetch_kite_holdings()
    kite_map      = {h["symbol"]: h for h in kite_holdings}

    if not kite_holdings:
        logger.warning("Kite holdings empty — cannot reconcile. Check token.")
        return {"status": "kite_unavailable"}

    now_str    = datetime.now(IST).isoformat()
    mismatches = []
    kite_only  = []
    db_only    = []
    matched    = 0

    # ── Check each Kite position against DB ──────────────────────────────────
    for sym, kite_pos in kite_map.items():
        kite_qty   = kite_pos["qty"]
        last_price = kite_pos["last_price"]

        if sym not in db_map:
            # Position in Kite but not in Supabase
            kite_only.append(sym)
            msg = (
                f"🚨 <b>UNKNOWN POSITION — {sym}</b>\n"
                f"In Kite: {kite_qty} shares @ ₹{kite_pos['avg_price']:,.2f}\n"
                f"Not found in TradeOS open_positions\n"
                f"<b>Manual review required</b>"
            )
            send_telegram(msg)
            log_anomaly(sb, sym, "reconcile_kite_only", "ERROR",
                        f"kite_qty={kite_qty}", f"{sym} in Kite but not in DB open_positions")
        else:
            db_qty = int(db_map[sym].get("actual_qty") or 0)

            if db_qty != kite_qty:
                mismatches.append({"symbol": sym, "db_qty": db_qty, "kite_qty": kite_qty})
                msg = (
                    f"⚠️ <b>QTY MISMATCH — {sym}</b>\n"
                    f"TradeOS: {db_qty} shares | Kite: {kite_qty} shares\n"
                    f"Diff: {kite_qty - db_qty:+d} shares\n"
                    f"<i>Update open_positions manually if trade was partial/adjusted</i>"
                )
                send_telegram(msg)
                log_anomaly(sb, sym, "reconcile_qty_mismatch", "WARN",
                            f"db={db_qty}, kite={kite_qty}",
                            f"{sym} qty mismatch: DB={db_qty} vs Kite={kite_qty}")
            else:
                matched += 1

            # Update current_price and reconcile metadata
            try:
                sb.table("open_positions").update({
                    "current_price":       last_price,
                    "kite_qty":            kite_qty,
                    "reconcile_status":    "QTY_MISMATCH" if db_qty != kite_qty else "MATCHED",
                    "last_reconciled_at":  now_str,
                }).eq("symbol", sym).execute()
            except Exception as e:
                logger.warning(f"Could not update reconcile for {sym}: {e}")

    # ── Check each DB position that's NOT in Kite ────────────────────────────
    for sym, db_pos in db_map.items():
        if sym not in kite_map:
            db_only.append(sym)
            msg = (
                f"🟠 <b>MISSING FROM KITE — {sym}</b>\n"
                f"TradeOS shows {db_pos.get('actual_qty')} shares open\n"
                f"Not found in Kite holdings\n"
                f"<i>Position may have been closed manually or via GTT</i>"
            )
            send_telegram(msg)
            log_anomaly(sb, sym, "reconcile_db_only", "WARN",
                        f"db_qty={db_pos.get('actual_qty')}",
                        f"{sym} in DB but not in Kite holdings")

            try:
                sb.table("open_positions").update({
                    "reconcile_status":   "DB_ONLY",
                    "last_reconciled_at": now_str,
                }).eq("symbol", sym).execute()
            except Exception:
                pass

    # ── Summary ───────────────────────────────────────────────────────────────
    total = len(db_map)
    has_issues = mismatches or kite_only or db_only

    summary = (
        f"{'⚠️' if has_issues else '✅'} <b>Kite Reconciliation</b>\n"
        f"Matched: {matched}/{total}\n"
        f"Qty mismatches: {len(mismatches)}\n"
        f"Kite-only (unknown): {len(kite_only)}\n"
        f"DB-only (possibly closed): {len(db_only)}"
    )
    if has_issues:
        send_telegram(summary)

    logger.info(
        f"Reconciliation complete: {matched} matched | "
        f"{len(mismatches)} qty mismatches | "
        f"{len(kite_only)} kite-only | "
        f"{len(db_only)} db-only"
    )

    return {
        "status":        "ok",
        "matched":       matched,
        "qty_mismatches": len(mismatches),
        "kite_only":     len(kite_only),
        "db_only":       len(db_only),
        "details": {
            "mismatches": mismatches,
            "kite_only":  kite_only,
            "db_only":    db_only,
        }
    }


if __name__ == "__main__":
    import json
    result = main()
    print(json.dumps(result, indent=2))
