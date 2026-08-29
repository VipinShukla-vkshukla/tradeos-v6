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
    is_kill_switch_active, cfg, cfg_bool, cfg_float, capital_for,
)

MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_open() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ENTRY_SIGNAL_TYPES — same set generate_signals.py itself uses to mean
# "this row is an actual entry candidate", not the AI's now-removed tier
# label. 29-Aug-2026: candidate_watch_tiers/ai_tier filtering is retired —
# see module docstring update below and the AI redesign this session's own
# FINDINGS.md entry (ai_tier/ai_conviction were measured inversely
# predictive, not merely unhelpful). Duplicated as a literal set, not
# imported, deliberately: generate_signals.py is the evening pipeline,
# this is a market-hours cron on a completely different schedule, and this
# project's own boundary discipline (Track D/E) is "a value handed across,
# never a function/constant imported across a scheduling boundary" — a
# literal copy here can't be silently changed by an unrelated edit there.
ENTRY_SIGNAL_TYPES = ("BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
                      "REENTRY_SETUP", "STAGED_ENTRY", "MOMENTUM_CONTINUATION")


def _alerting_actions() -> set[str]:
    return {a.strip() for a in cfg("candidate_alert_on_actions", "BUY_NOW,CHASE_LIMIT").split(",") if a.strip()}


def load_candidates(sb, trade_date: str) -> list[dict]:
    """
    Today's ranked candidates, from the immutable decision snapshot.

    Filters on signal_type (the deterministic, gate-passed classification
    generate_signals.py itself assigns), not ai_tier — 29-Aug-2026. The old
    filter (candidate_watch_tiers, default TIER_1/TIER_2) watched AI's
    self-rated highest-conviction candidates specifically; this session
    measured that bucket UNDERPERFORMING its own lower tiers on real
    resolved outcomes. Watching by signal_type means every genuine entry
    candidate is covered, not a subset selected by a since-removed,
    inversely-predictive label.
    """
    try:
        rows = (sb.table("signal_output_daily")
                  .select("symbol,sector,industry,strategy,current_price,"
                          "entry_zone_low,entry_zone_high,planned_stop,planned_target,"
                          "planned_risk_pct,implied_rr,ai_max_chase_pct")
                  .eq("date", trade_date)
                  .in_("signal_type", ENTRY_SIGNAL_TYPES)
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
        logger.info(f"  No entry candidates for {trade_date}")
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
            # capital_for("SWING"), not the pooled TOTAL_CAPITAL: this monitor
            # is the swing book, and once intraday goes LIVE its sleeve is not
            # swing's to size against. While intraday is PAPER the two are the
            # same number, so this changes nothing today and stops being wrong
            # on the day the switch flips — which is the day nobody would think
            # to re-check a sizing call in a monitor.
            total_capital=capital_for("SWING"),
            open_positions=open_pos,
            regime=regime,
            min_rr=min_rr,
            max_chase_pct=c.get("ai_max_chase_pct"),
        )

        prev   = state.get(sym, {})
        prev_a = prev.get("last_action")
        changed = prev_a != d.action

        upserts.append({
            # ai_tier column left null going forward — the label it recorded
            # is retired, not replaced with a same-shaped stand-in.
            "trade_date": trade_date, "symbol": sym, "ai_tier": None,
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

    sent = _maybe_send_candidate_alerts(sb, alerts, source)

    logger.info(f"  Candidate monitor: {evaluated} evaluated | "
               f"{len(alerts)} actionable | {len(alerts) if sent else 0} sent")
    return {"status": "ok", "watched": evaluated, "alerts": len(alerts),
            "sent": len(alerts) if sent else 0, "source": source}


def _daemon_lease_healthy(view) -> bool:
    """
    Pure. True if the real daemon (intraday/engine.py) currently holds its
    lease — from this script's own perspective (it never calls acquire(),
    so any lease held is held by someone else).

    `view.held_by_other` already means "a DIFFERENT process holds it and it
    has NOT expired" (intraday/lease.py's own LeaseView docstring), which is
    exactly the daemon-alive question this script needs answered. An
    unreadable lease (`view.held_by_other is False` on the error path too)
    reads as unhealthy here, on purpose — the same "over-watching is
    recoverable, silently watching nothing is not" rule this project
    already applies elsewhere: a DB blip must not read as permission for
    this monitor to go quiet.
    """
    return bool(getattr(view, "held_by_other", False))


def _maybe_send_candidate_alerts(sb, alerts: list, source: str) -> bool:
    """
    Send `alerts` unless the daemon's lease says it already covers this —
    26-Aug-2026, Phase 2b of the swing framework evolution blueprint
    (docs/PHASE4_RED_TEAM.md's "C2 — 'One allocator' is false" finding,
    second of its two named entry paths; Phase 2a closed the third,
    control/execution_engine.py). This monitor runs on a GitHub Actions
    cron, entirely outside the daemon and its lease — a real "second actor"
    evaluating the identical question (is this candidate buyable now) with
    a FLAT min_rr_to_enter bar that never scales for regime, unlike the
    daemon's regime_min_rr(). BLUEJET, 26-Aug: never once reached the
    daemon's own allocator scoring (zero rows in allocation_decisions,
    ever) because it never cleared the daemon's regime-scaled bar — yet
    this monitor alerted it repeatedly as BUY_NOW on its looser flat one,
    all morning, for a trade this monitor has no way to place anyway (it
    is alert-only; the daemon is the only path that can actually buy). The
    daemon is a strict superset of this monitor's job: real-time
    KiteTicker prices vs. this script's fetch_live_prices() fallback,
    regime-scaled vs. flat, allocator-aware, a 15s loop vs. a jittery
    30-min cron. So: while the daemon's lease is healthy, this monitor
    stays silent — candidate_watch is still written by the caller either
    way, for audit — because the daemon already covers it, better. Only
    when the lease looks stale or absent (a real daemon outage) does this
    fire, and it says so plainly, so the operator is never silently blind
    during an outage.

    Returns True if `alerts` was actually sent.
    """
    if not alerts:
        return False
    from intraday.lease import observe as _observe_daemon_lease
    view = _observe_daemon_lease(sb)
    if _daemon_lease_healthy(view):
        logger.info(f"  {len(alerts)} alert(s) suppressed — daemon lease "
                   f"held by {view.holder}@{view.hostname}, already covers "
                   f"this on real-time prices and a regime-scaled bar")
        return False

    # BLUEJET fix, 29-Aug-2026 — the daemon is down, so this fallback is
    # about to fire on its own flat bar, the exact shape that already went
    # wrong once. Before firing, check whether the REAL system (while it
    # was still up today) ever actually scored this symbol — reusing that
    # decision, not re-deriving one. A symbol with zero allocation_decisions
    # rows today is precisely BLUEJET's own signature; a symbol whose last
    # real verdict was DECLINE means the allocator already said no on
    # sharper information than this fallback has. Only a last-known TAKE,
    # or a symbol the daemon never reached (ambiguous, not a red flag on
    # its own — see below), gets through.
    confirmed, blocked = [], []
    try:
        # PAGED. allocation_decisions is a known-large table (F-88 this
        # session measured 69,247 rows in a WEAK bucket alone) and a
        # heavily-repeat-logged symbol-day can carry 100+ rows on its own
        # (27-Aug ledger entry) — even a short `alerts` symbol list is not
        # a safe bound against PostgREST's 1000-row cap. order_by="id",
        # this table's own proven unique key (test_static_analysis.py's
        # _FETCH_ALL_SORT_KEY), not decided_at — re-sorted by decided_at
        # in Python below, after every page is in hand.
        from config import fetch_all
        syms = [d.symbol for d in alerts]
        rows = fetch_all(lambda: sb.table("allocation_decisions")
                         .select("symbol,verdict,decided_at")
                         .eq("framework", "SWING").in_("symbol", syms)
                         .gte("trade_date", str(today_ist())),
                         order_by="id")
        rows.sort(key=lambda r: r.get("decided_at") or "", reverse=True)
        last_verdict = {}
        for r in rows:
            last_verdict.setdefault(r["symbol"], r["verdict"])   # first = latest, sorted above
        for d in alerts:
            v = last_verdict.get(d.symbol)
            if v == "DECLINE":
                blocked.append((d.symbol, "allocator's last real verdict today was DECLINE"))
            else:
                confirmed.append(d)   # TAKE, DEFER, or never reached — see docstring
    except Exception as e:
        logger.warning(f"  allocation_decisions check failed ({e}) — firing "
                       f"unfiltered, degraded-fallback-of-a-degraded-fallback")
        confirmed = alerts

    if blocked:
        logger.warning(f"  {len(blocked)} alert(s) suppressed — real allocator "
                       f"already declined them today: "
                       + ", ".join(f"{s} ({why})" for s, why in blocked))
    if not confirmed:
        return False

    logger.warning(f"  daemon lease not held ({view.detail}) — firing "
                  f"{len(confirmed)} alert(s) as a degraded fallback, flat "
                  f"min_rr bar, not regime-scaled")
    _send(confirmed, source, degraded=True)
    return True


def _send(decisions: list, source: str, degraded: bool = False):
    """One consolidated message. Thirteen cycles a day means brevity matters."""
    icon = {"BUY_NOW": "🟢", "CHASE_LIMIT": "🟡"}
    lines = (["<b>⚠️ Entry Signal — daemon appears down, degraded monitor</b>",
              "<i>Flat R:R bar, not regime-scaled — the daemon's own check is "
              "stricter. This channel cannot place orders.</i>", ""]
             if degraded else
             ["<b>⚡ Entry Signal — live</b>", ""])
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
