"""
TradeOS v7 — Intraday Candidate Monitor
========================================
Watches today's ranked candidates against LIVE prices and alerts when the
decision changes.

WHAT WAS MISSING
----------------
The 30-minute monitor covered open_positions only. Candidates were evaluated
once, at 22:00, against the closing price — so a TIER_1 that traded into its
entry zone at 11:15 produced nothing, and one that gapped past its target
produced no warning. The morning brief was the only candidate-facing message
of the day, and it described a price roughly 18 hours old.

That is the gap between "here is a list of stocks" and "here is a trade".

ALERTS ON CHANGE, NOT ON STATE
------------------------------
Running every 30 minutes means 13 evaluations per session. Alerting on state
would send the same message 13 times. candidate_watch stores the last action
per symbol per day, and only a TRANSITION notifies — a candidate entering its
zone, or losing its reward:risk. Actions that require nothing of you (WAIT,
SKIP) are recorded silently: a notification telling you to do nothing is worse
than no notification, because it teaches you to ignore the channel.

PRICE HONESTY
-------------
Kite is real-time. yfinance is ~15 minutes delayed. Both are usable for a
zone-proximity nudge; only Kite is usable for a stop decision. The source is
recorded and shown, so a delayed price is never presented as live.
"""

from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, today_ist, IST, DRY_RUN,
    is_kill_switch_active, cfg, cfg_bool, cfg_float, TOTAL_CAPITAL,
)

MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_open() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _watch_tiers() -> list[str]:
    return [t.strip() for t in cfg("candidate_watch_tiers", "TIER_1,TIER_2").split(",") if t.strip()]


def _alerting_actions() -> set[str]:
    return {a.strip() for a in cfg("candidate_alert_on_actions", "BUY_NOW,CHASE_LIMIT").split(",") if a.strip()}


def load_candidates(sb, trade_date: str) -> list[dict]:
    """Today's ranked candidates, from the immutable decision snapshot."""
    tiers = _watch_tiers()
    try:
        rows = (sb.table("signal_output_daily")
                  .select("symbol,sector,industry,ai_tier,ai_conviction,current_price,"
                          "entry_zone_low,entry_zone_high,planned_stop,planned_target,"
                          "planned_risk_pct,implied_rr,ai_max_chase_pct")
                  .eq("date", trade_date)
                  .in_("ai_tier", tiers)
                  .execute().data) or []
        return rows
    except Exception as e:
        logger.warning(f"  candidate load failed: {e}")
        return []


def _load_state(sb, trade_date: str) -> dict[str, dict]:
    try:
        rows = (sb.table("candidate_watch").select("*")
                  .eq("trade_date", trade_date).execute().data) or []
        return {r["symbol"]: r for r in rows}
    except Exception as e:
        logger.warning(f"  candidate_watch unreadable ({e}) — run migration 009")
        return {}


def run(sb=None, trade_date: str | None = None, require_live: bool = True) -> dict:
    if is_kill_switch_active():
        return {"status": "skipped", "reason": "kill_switch"}
    if not cfg_bool("candidate_watch_enabled", True):
        return {"status": "disabled"}

    sb = sb or get_supabase()
    if trade_date is None:
        from config import get_trade_date
        trade_date = get_trade_date(sb, mode="auto", caller="candidate_monitor")

    cands = load_candidates(sb, trade_date)
    if not cands:
        logger.info(f"  No {'/'.join(_watch_tiers())} candidates for {trade_date}")
        return {"status": "ok", "watched": 0, "alerts": 0}

    from analysis.trade_decision import decide, fetch_live_prices

    symbols = [c["symbol"] for c in cands if c.get("symbol")]
    prices, source = fetch_live_prices(symbols)

    if not prices and require_live:
        logger.warning(
            "  No live price feed — skipping. Evaluating candidates against the "
            "previous close intraday would produce alerts about a price that no "
            "longer exists. Refresh Kite: python -m kite.token_manager --login-url"
        )
        return {"status": "no_live_prices", "watched": len(cands), "alerts": 0}

    logger.info(f"  Candidates: {len(cands)} | live prices: {len(prices)} via {source}")

    open_pos = (sb.table("open_positions").select("*").eq("status", "ACTIVE").execute().data) or []
    held     = {p["symbol"] for p in open_pos}
    regime   = "NEUTRAL"
    try:
        r = sb.table("market_regime").select("regime").eq("date", trade_date).limit(1).execute().data
        if r:
            regime = r[0].get("regime") or "NEUTRAL"
    except Exception:
        pass

    state   = _load_state(sb, trade_date)
    alerting = _alerting_actions()
    min_rr  = cfg_float("min_rr_to_enter", 1.0)

    alerts, evaluated, upserts = [], 0, []
    now_iso = datetime.now(IST).isoformat()

    for c in cands:
        sym = c.get("symbol")
        if not sym or sym in held:
            continue      # already in the book — position monitor owns it
        evaluated += 1

        d = decide(
            c, prices.get(sym),
            total_capital=TOTAL_CAPITAL,
            open_positions=open_pos,
            regime=regime,
            min_rr=min_rr,
            max_chase_pct=c.get("ai_max_chase_pct"),
        )

        prev   = state.get(sym, {})
        prev_a = prev.get("last_action")
        changed = prev_a != d.action

        upserts.append({
            "trade_date": trade_date, "symbol": sym, "ai_tier": c.get("ai_tier"),
            "last_action": d.action, "last_headline": d.headline,
            "last_price": d.live_price, "last_rr": d.rr_live,
            "last_eval_at": now_iso, "price_source": source,
            "alerts_sent": int(prev.get("alerts_sent") or 0) + (1 if (changed and d.action in alerting) else 0),
            "last_alert_at": now_iso if (changed and d.action in alerting) else prev.get("last_alert_at"),
        })

        # Only a TRANSITION into an actionable state notifies.
        if changed and d.action in alerting:
            alerts.append(d)
            logger.success(f"  ALERT {d.action:<12} {d.headline}")
        elif changed:
            logger.info(f"  (silent) {sym}: {prev_a or 'new'} -> {d.action}")

    if upserts and not DRY_RUN:
        try:
            for i in range(0, len(upserts), 50):
                sb.table("candidate_watch").upsert(
                    upserts[i:i + 50], on_conflict="trade_date,symbol").execute()
        except Exception as e:
            logger.warning(f"  candidate_watch write failed: {e}")

    if alerts:
        _send(alerts, source)

    logger.info(f"  Candidate monitor: {evaluated} evaluated | {len(alerts)} alert(s)")
    return {"status": "ok", "watched": evaluated, "alerts": len(alerts), "source": source}


def _send(decisions: list, source: str):
    """One consolidated message. Thirteen cycles a day means brevity matters."""
    icon = {"BUY_NOW": "🟢", "CHASE_LIMIT": "🟡"}
    lines = ["<b>⚡ Entry Signal — live</b>", ""]
    for d in decisions:
        lines.append(f"{icon.get(d.action, '•')} <b>{d.symbol}</b> @ ₹{d.live_price:,.2f}")
        lines.append(f"    {d.reason}")
        if d.qty:
            lines.append(f"    <code>{d.qty} sh ≈ ₹{d.invested:,.0f} · risk ₹{d.risk_amount:,.0f}</code>")
        lines.append("")
    lines.append(
        f"<i>Price source: {source}"
        + ("  ⚠️ ~15 min delayed" if source == "yfinance" else " (real-time)")
        + "</i>"
    )
    try:
        from alerts.send_alerts import send_message
        send_message("\n".join(lines), subject_suffix="Entry Signal")
    except Exception as e:
        logger.warning(f"  candidate alert send failed: {e}")


def main(require_live: bool = True) -> dict:
    if not is_market_open():
        logger.info("Market closed — candidate monitor skipping")
        return {"status": "market_closed"}
    logger.info("=" * 60)
    logger.info("Candidate Monitor" + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 60)
    return run(require_live=require_live)


if __name__ == "__main__":
    import argparse, os
    ap = argparse.ArgumentParser(description="TradeOS v7 — intraday candidate monitor")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force",   action="store_true", help="Run outside market hours")
    ap.add_argument("--allow-stale", action="store_true",
                    help="Evaluate against the last close when no live feed is available")
    a = ap.parse_args()
    if a.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(run(require_live=not a.allow_stale) if a.force else main(require_live=not a.allow_stale))
