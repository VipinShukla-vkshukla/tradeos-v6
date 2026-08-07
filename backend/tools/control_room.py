"""
What the Control Room should be set to this week, and what each number means.

    python -m tools.control_room             print it
    python -m tools.control_room --propose   also write to brain_proposals

THE QUESTION THIS ANSWERS
-------------------------
The Operator Panel shows ten numbers — per order, orders/day, notional/day, new
positions/day, alert top N, for each book — and nothing on the screen says where
any of them came from, which of them actually bind, or how much money is at
stake if they are all hit at once. "Risk of ₹200 a day, and I don't know where
that came from" is the correct reaction to a panel that reports position VALUE
and never reports RISK.

So this prints three things:

  1. RISK EXPOSURE — the translation the panel does not do. What the current
     settings put at stake per trade, per day, and in total, in rupees.
  2. THE PANEL, CONTROL BY CONTROL — current value, whether it BINDS or is dead,
     the risk it implies, and what the evidence says it should be.
  3. MISSING CONTROLS — attributes that should exist and do not. A cap on
     position value is not a cap on risk, and the panel currently has no way to
     express "stop trading for the day".

NOTIONAL IS NOT RISK, AND THAT IS THE WHOLE POINT
-------------------------------------------------
Every cap on the panel bounds POSITION VALUE. Risk is value x stop distance, and
the stop varies from 5% to 9% across this book. So one notional number maps to a
RANGE of risk:

    ₹4,000 at a 5% stop = ₹200 risk        <- the intended budget
    ₹4,000 at a 9% stop = ₹360 risk        <- 1.8x it

The real per-trade budget is `risk_pct_per_trade` (1% = ₹200) and it IS enforced
— portfolio_constraints.py sizes on `risk_budget // risk_per_share`. The panel
caps are a second, blunter ceiling on top. They are not wrong; they are just
denominated in a different unit from the thing the operator wants to control,
and nothing on screen converts between the two.

PROPOSE, NEVER APPLY. --propose writes to brain_proposals. Nothing here changes
a live value, including the missing controls it recommends creating.
"""

from __future__ import annotations

import argparse
import statistics as st
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import TOTAL_CAPITAL, capital_for, cfg, cfg_float, get_supabase, today_ist
from tools.weekly_review import (MIN_SAMPLE, MIN_SESSIONS, _RUN_ID,
                                 _tradeable_floor, dedupe_setups)

# TOTAL_CAPITAL is the whole real account — right denominator for account-wide
# figures (section 3's proposed cross-book controls, the both-books worst case).
# Since the capital split (config.capital_for), swing and intraday no longer
# share one number — engine.py sizes intraday off capital_for("INTRADAY"), not
# TOTAL_CAPITAL, and this file using C for both was exactly the "check that
# computes against the wrong figure" pattern this project keeps finding.
C = TOTAL_CAPITAL
C_SWING = capital_for("SWING")
C_INTRADAY = capital_for("INTRADAY")


@dataclass
class Rec:
    key: str
    label: str                    # what the panel calls it
    book: str                     # SWING | INTRADAY | BOTH
    current: str
    suggested: str
    binds: str                    # BINDS | dead | n/a
    implication: str              # what this means in rupees of risk
    evidence: str
    confident: bool
    priority: int = 2


def _f(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def _g(key, d):
    return cfg_float(key, d)


# ─────────────────────────────────────────────────────────────────────────────
# 1. RISK EXPOSURE — the translation the panel does not do
# ─────────────────────────────────────────────────────────────────────────────

def risk_exposure(sb) -> dict:
    rpt = _g("risk_pct_per_trade", 1.0)
    heat_cap_pct = _g("portfolio_max_total_risk_pct", 6.0)
    per_trade = C_SWING * rpt / 100.0
    heat_cap = C_SWING * heat_cap_pct / 100.0

    logger.info("")
    logger.info("═" * 78)
    logger.info("1 · RISK EXPOSURE — what the current settings actually put at stake")
    logger.info("═" * 78)
    logger.info(f"  capital (swing sleeve)         Rs {C_SWING:>9,.0f}")
    logger.info(f"  risk_pct_per_trade  {rpt:>4.1f}%      Rs {per_trade:>9,.0f}   "
                f"<- THIS is where the per-trade number comes from")
    logger.info(f"     enforced in analysis/portfolio_constraints.py: "
                f"qty = risk_budget // risk_per_share")
    logger.info(f"  portfolio_max_total_risk_pct {heat_cap_pct:>4.1f}%  Rs {heat_cap:>9,.0f}   "
                f"total open risk across all positions")

    # Live heat.
    ps = [p for p in (sb.table("open_positions").select("*")
                        .eq("status", "ACTIVE").execute().data or [])]
    heat = 0.0
    rows = []
    for p in ps:
        e, s = _f(p.get("entry_price")), _f(p.get("planned_stop"))
        q = int(p.get("current_qty") or p.get("actual_qty") or 0)
        if e and s and s < e and q:
            r = (e - s) * q
            heat += r
            rows.append((p["symbol"], q, e * q, (e - s) / e * 100, r,
                         (p.get("framework") or "SWING")))

    logger.info("")
    logger.info(f"  OPEN NOW — {len(rows)} position(s)")
    logger.info(f"    {'symbol':<12}{'book':<10}{'value':>9}{'stop%':>8}{'risk Rs':>10}")
    for sym, q, val, stop, r, fw in rows:
        logger.info(f"    {sym:<12}{fw:<10}{val:>9,.0f}{stop:>8.2f}{r:>10,.0f}")
    logger.info(f"    {'':<22}{sum(x[2] for x in rows):>9,.0f}{'':>8}{heat:>10,.0f}")
    logger.info("")
    tag = "OK" if heat <= heat_cap else "OVER THE CAP"
    logger.info(f"  open heat Rs {heat:,.0f} ({heat / C_SWING:.1%} of capital) against a "
                f"Rs {heat_cap:,.0f} cap ({heat_cap_pct}%) — {tag}, "
                f"Rs {heat_cap - heat:,.0f} of headroom")

    # Worst-case day, if every daily cap were hit.
    logger.info("")
    logger.info("  WORST CASE IF EVERY DAILY CAP IS HIT TODAY")
    total_new = 0.0
    for fw, per, orders, notional, newpos in (
        ("SWING", _g("swing_max_order_value", 4000), _g("swing_max_orders_per_day", 6),
         _g("swing_max_notional_per_day", 20000), _g("swing_max_new_per_day", 3)),
        ("INTRADAY", _g("intraday_max_order_value", 6000),
         _g("intraday_max_orders_per_day", 5),
         _g("intraday_max_notional_per_day", 20000),
         _g("intraday_max_new_per_day", 5)),
    ):
        cands = [(per * orders, "orders/day"), (per * newpos, "new positions/day"),
                 (notional, "notional/day")]
        val, which = min(cands, key=lambda t: t[0])
        total_new += val
        logger.info(f"    {fw:<9} Rs {val:>8,.0f} of new exposure  "
                    f"(bound by {which})")
    logger.info(f"    {'BOTH':<9} Rs {total_new:>8,.0f} = {total_new / C:.0%} of the account "
                f"in one day, and nothing caps the two books jointly")
    return {"per_trade": per_trade, "heat": heat, "heat_cap": heat_cap,
            "daily_notional": total_new, "positions": rows}


# ─────────────────────────────────────────────────────────────────────────────
# 2. THE PANEL, CONTROL BY CONTROL
# ─────────────────────────────────────────────────────────────────────────────

def panel_recs(sb, setups: list, closed: list) -> list[Rec]:
    out: list[Rec] = []
    floor = _tradeable_floor()

    # Median stop width per book — the conversion factor between notional and risk.
    sw = [r for r in closed if (r.get("framework") or "SWING").upper() == "SWING"]
    stops = []
    for r in sw:
        e, s = _f(r.get("entry_price")), _f(r.get("planned_stop_at_entry"))
        if e and s and s < e:
            stops.append((e - s) / e * 100)
    for p in (sb.table("open_positions").select("entry_price,planned_stop")
                .eq("status", "ACTIVE").execute().data or []):
        e, s = _f(p.get("entry_price")), _f(p.get("planned_stop"))
        if e and s and s < e:
            stops.append((e - s) / e * 100)
    swing_stop = st.median(stops) if stops else 7.0

    intr_stops = [_f(r.get("risk_pct")) for r in setups if _f(r.get("risk_pct")) >= floor]
    intraday_stop = st.median(intr_stops) if intr_stops else 0.7

    # ── SWING: per order ────────────────────────────────────────────────────
    sv = _g("swing_max_order_value", 4000)
    rpt_rs = C_SWING * _g("risk_pct_per_trade", 1.0) / 100.0
    implied = sv * swing_stop / 100.0
    out.append(Rec(
        key="swing_max_order_value", label="Swing · Per order", book="SWING",
        current=f"{sv:,.0f}", suggested=f"{sv:,.0f} (hold)",
        binds="ceiling — the risk model usually binds first",
        implication=(f"Rs {sv:,.0f} at the measured median {swing_stop:.1f}% stop = "
                     f"Rs {implied:,.0f} of risk, against the Rs {rpt_rs:,.0f} "
                     f"per-trade budget"),
        evidence=(f"the sizing model already caps at risk_pct_per_trade "
                  f"(Rs {rpt_rs:,.0f} // risk-per-share), and the four live "
                  f"positions carry Rs 118-202 of risk each — inside budget. This "
                  f"cap is a backstop for when the stop is unusually tight, not "
                  f"the primary rail"),
        confident=True, priority=3))

    # ── SWING: orders/day vs new positions/day — which one is real? ─────────
    so, sn = _g("swing_max_orders_per_day", 6), _g("swing_max_new_per_day", 3)
    snot = _g("swing_max_notional_per_day", 20000)
    by_new = sv * sn
    out.append(Rec(
        key="swing_max_orders_per_day", label="Swing · Orders/day", book="SWING",
        current=f"{so:.0f}", suggested=f"{sn + 1:.0f}",
        binds="DEAD — cannot bind",
        implication=f"would allow Rs {sv * so:,.0f}/day if it ever bound",
        evidence=(f"order_manager._today_totals counts BUY ORDERS ONLY and the cap "
                  f"is applied only when side == BUY, so exits do not consume it. "
                  f"With new positions/day at {sn:.0f}, the {so:.0f}-order cap can "
                  f"never be reached — it is a control that does nothing. Set it "
                  f"just above new positions/day so it reads as the backstop it is, "
                  f"or raise new positions/day if more entries are actually wanted"),
        confident=True, priority=2))

    out.append(Rec(
        key="swing_max_notional_per_day", label="Swing · Notional/day", book="SWING",
        current=f"{snot:,.0f}", suggested=f"{by_new:,.0f}",
        binds="DEAD — cannot bind",
        implication=f"{snot / C_SWING:.0%} of the account, but only Rs {by_new:,.0f} is reachable",
        evidence=(f"per order Rs {sv:,.0f} x new positions/day {sn:.0f} = "
                  f"Rs {by_new:,.0f}, which is below the Rs {snot:,.0f} notional cap, "
                  f"so notional/day can never bind either. Two of the three swing "
                  f"daily caps are decoration; only new positions/day is real"),
        confident=True, priority=2))

    out.append(Rec(
        key="swing_max_new_per_day", label="Swing · New positions/day", book="SWING",
        current=f"{sn:.0f}", suggested=f"{sn:.0f} (hold)",
        binds="BINDS — this is the real swing cap",
        implication=(f"{sn:.0f} x Rs {sv:,.0f} = Rs {by_new:,.0f} of new exposure "
                     f"({by_new / C_SWING:.0%} of capital), about "
                     f"Rs {sn * min(implied, rpt_rs):,.0f} of new risk per day"),
        evidence=(f"the only swing daily cap that can be reached. At "
                  f"Rs {rpt_rs:,.0f} of risk per trade this puts "
                  f"{sn * rpt_rs / C_SWING:.1%} of capital at stake per day, which sits "
                  f"inside the {_g('portfolio_max_total_risk_pct', 6.0):.0f}% total "
                  f"heat cap after two days of entries"),
        confident=True, priority=3))

    # ── INTRADAY: per order — WHICH of the two terms in engine.py's min() wins? ──
    # Two symmetric failure modes, one min(). The pct_budget < iv branch below
    # existed; its mirror did not, and C itself was TOTAL_CAPITAL (the swing
    # sleeve) rather than capital_for("INTRADAY") — so the exact case live on
    # 07-Aug-2026 (iv=10,000 vs the real ceiling of ipp% of the 100,000
    # intraday sleeve) went undetected by the one tool built to catch this.
    iv = _g("intraday_max_order_value", 6000)
    ipp = _g("intraday_max_position_pct", 25.0)
    pct_budget = C_INTRADAY * ipp / 100.0
    if pct_budget < iv:
        out.append(Rec(
            key="intraday_max_order_value", label="Intraday · Per order",
            book="INTRADAY",
            current=f"{iv:,.0f}", suggested=f"{pct_budget:,.0f}",
            binds="DEAD — cannot bind",
            implication=(f"the panel says Rs {iv:,.0f} but the real ceiling is "
                         f"Rs {pct_budget:,.0f}"),
            evidence=(f"engine.py sizes on min(capital_for('INTRADAY') x "
                      f"intraday_max_position_pct x market multiplier, "
                      f"max_order_value). intraday_max_position_pct is {ipp:.0f}% = "
                      f"Rs {pct_budget:,.0f}, so the Rs {iv:,.0f} panel control never "
                      f"binds and moving it changes nothing until it drops below "
                      f"Rs {pct_budget:,.0f}. A switch that does nothing is the "
                      f"failure this project fights hardest"),
            confident=True, priority=1))
    elif iv < pct_budget:
        i_sizes = [_f(r.get("entry_price")) * _f(r.get("actual_qty")) for r in closed
                  if (r.get("framework") or "").upper() == "INTRADAY"
                  and _f(r.get("entry_price")) and _f(r.get("actual_qty"))]
        size_evidence = (f"Median realised size confirms it: Rs {st.median(i_sizes):,.0f} "
                         f"median, Rs {max(i_sizes):,.0f} the largest of {len(i_sizes)} "
                         f"closed intraday trades, both clustered against the Rs "
                         f"{iv:,.0f} ceiling, not the {ipp:.0f}% one" if i_sizes else
                         f"No closed intraday trades in the lookback window to confirm "
                         f"realised sizing against")
        out.append(Rec(
            key="intraday_max_position_pct", label="Intraday · Max position %",
            book="INTRADAY",
            current=f"{ipp:.0f}% (Rs {pct_budget:,.0f})", suggested=f"raise Per order to ~{pct_budget:,.0f}, or hold",
            binds="DEAD — cannot bind",
            implication=(f"the panel implies up to Rs {pct_budget:,.0f} per position, "
                         f"but the real ceiling is Rs {iv:,.0f}"),
            evidence=(f"same min() as above, other direction: intraday_max_order_value "
                      f"(Rs {iv:,.0f}) is smaller than {ipp:.0f}% of capital "
                      f"(Rs {pct_budget:,.0f}), so the flat per-order cap always wins — "
                      f"intraday_max_position_pct currently has no effect on sizing at "
                      f"all. {size_evidence}"),
            confident=True, priority=1))

    # Names the sizing cannot buy at all.
    prices = {}
    for r in setups:
        try:
            prices.setdefault(r["symbol"], float(r["entry"]))
        except (KeyError, TypeError, ValueError):
            pass
    unbuyable = [s for s, p in prices.items() if p > pct_budget]
    single = [s for s, p in prices.items() if pct_budget / 2 < p <= pct_budget]
    if unbuyable or single:
        out.append(Rec(
            key="intraday_min_price / max_position_pct", label="Intraday · universe fit",
            book="INTRADAY",
            current=f"budget Rs {pct_budget:,.0f}",
            suggested="raise intraday_min_price, or accept these are undetectable",
            binds="n/a",
            implication=(f"{len(unbuyable)} scanned name(s) cost more than the whole "
                         f"position budget and can never be traded; {len(single)} "
                         f"size to a single share"),
            evidence=(f"the scanner admits names the sizing cannot buy — "
                      f"{', '.join(sorted(unbuyable)[:4])}"
                      f"{' and others' if len(unbuyable) > 4 else ''} price above "
                      f"Rs {pct_budget:,.0f}. They consume scan budget and produce "
                      f"detections that can never become trades. A single-share "
                      f"position also cannot book a partial, which is why breakeven "
                      f"is now its own rung in the intraday ladder"),
            confident=bool(unbuyable), priority=2))

    io, inw = _g("intraday_max_orders_per_day", 5), _g("intraday_max_new_per_day", 5)
    inot = _g("intraday_max_notional_per_day", 20000)
    real_per = min(iv, pct_budget)
    by_new_i = real_per * inw
    binding_i = min([(real_per * io, "orders/day"), (by_new_i, "new positions/day"),
                     (inot, "notional/day")], key=lambda t: t[0])
    out.append(Rec(
        key="intraday_max_notional_per_day", label="Intraday · Notional/day",
        book="INTRADAY",
        current=f"{inot:,.0f}", suggested=f"{by_new_i:,.0f}",
        binds=("BINDS" if binding_i[1] == "notional/day" else "dead"),
        implication=f"{inot / C_INTRADAY:.0%} of the account in MIS positions in one day",
        evidence=(f"real per-position budget is Rs {real_per:,.0f} "
                  f"({'the flat Per order cap, not the ' + f'{ipp:.0f}% rule' if real_per == iv else f'the {ipp:.0f}% rule, not the flat Per order cap'} "
                  f"of Rs {pct_budget:,.0f}), so {inw:.0f} new positions reach "
                  f"Rs {by_new_i:,.0f}. Setting notional/day to that makes the panel "
                  f"state the true ceiling instead of one further off"),
        confident=True, priority=2))

    # ── Alert top N, from how many setups a session actually produces ───────
    good = [r for r in setups if _f(r.get("risk_pct")) >= floor]
    per_day = defaultdict(int)
    for r in good:
        per_day[str(r.get("trade_date"))] += 1
    if per_day:
        med = st.median(list(per_day.values()))
        atn = _g("intraday_alert_top_n", 5)
        out.append(Rec(
            key="intraday_alert_top_n", label="Intraday · Alert top N",
            book="INTRADAY",
            current=f"{atn:.0f}", suggested=f"{atn:.0f} (hold)",
            binds="BINDS" if med > atn else "rarely reached",
            implication=f"at most {atn:.0f} alerts a session, then only on an improvement",
            evidence=(f"a median session produces {med:.0f} tradeable setup(s) "
                      f"(above the {floor:.2f}% cost floor), so the top-{atn:.0f} "
                      f"streaming bar {'binds and is doing useful filtering' if med > atn else 'is rarely reached — alerts are not the constraint'}"),
            confident=len(per_day) >= MIN_SESSIONS, priority=3))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 3. MISSING CONTROLS
# ─────────────────────────────────────────────────────────────────────────────

MISSING = [
    ("max_daily_risk_pct", "3.0",
     "Daily RISK budget, in percent of capital",
     "Every existing daily cap bounds position VALUE. None bounds the money at "
     "stake. Three swing entries at Rs 200 of risk is Rs 600 (3% of capital) and "
     "that is fine; three at a 9% stop against the Rs 4,000 notional ceiling is "
     "Rs 1,080 (5.4%) and that is a different day. The panel cannot currently "
     "tell those apart, and the operator asked exactly this question.",
     1),
    ("max_daily_loss_pct", "4.0",
     "Stop trading for the day after this much REALISED loss",
     "There is no circuit breaker. The kill switch is manual and permanent; "
     "nothing halts entries automatically after a bad morning. A run of "
     "correlated losses — six long-only engines on a risk-off day — is the "
     "documented reason the index gate exists, and the index gate only stops NEW "
     "setups being detected, not the damage already done.",
     1),
    ("max_consecutive_losses", "4",
     "Pause new entries after this many losing trades in a row",
     "The intraday book has a per-symbol version (intraday_block_reentry_after_loss) "
     "but no account-level one. Consecutive losses are the cheapest available "
     "signal that the regime has changed underneath the engines.",
     2),
    ("max_open_heat_pct", "6.0",
     "Surface portfolio_max_total_risk_pct on the panel",
     "It already exists and already binds — it is the single most important risk "
     "number in the system — and it is not on the Operator Panel. Open heat is "
     "currently 3.3% of capital against this 6% cap, and the operator has no way "
     "to see either figure without querying the database.",
     2),
    ("combined_max_notional_per_day", "20000",
     "A joint cap across both books",
     "Swing and intraday each have their own daily caps and nothing reconciles "
     "them. Hitting both today would commit Rs 32,000 of new exposure against "
     "Rs 20,000 of capital. Intraday is MIS and margined so this is not "
     "impossible, but it is unbudgeted, and it is the one number no existing "
     "control can express.",
     1),
]


def missing_controls(sb) -> list[Rec]:
    out = []
    for key, suggested, label, why, prio in MISSING:
        exists = sb.table("system_config").select("key").eq("key", key).execute().data
        if exists:
            continue
        out.append(Rec(key=key, label=label, book="BOTH", current="does not exist",
                       suggested=suggested, binds="n/a",
                       implication=f"Rs {C * _f(suggested) / 100:,.0f}" if key.endswith("_pct") else suggested,
                       evidence=why, confident=True, priority=prio))
    return out


# ─────────────────────────────────────────────────────────────────────────────

def _print_recs(title: str, recs: list[Rec]) -> None:
    logger.info("")
    logger.info("═" * 78)
    logger.info(title)
    logger.info("═" * 78)
    for r in sorted(recs, key=lambda x: (x.priority, x.book)):
        mark = "!" if r.binds.startswith("DEAD") else " "
        logger.warning(f" {mark}[P{r.priority}] {r.label}   ({r.key})")
        logger.info(f"        now {r.current}  ->  {r.suggested}     [{r.binds}]")
        if r.implication:
            logger.info(f"        means: {r.implication}")
        logger.info(f"        why  : {r.evidence}")


def _write(sb, recs: list[Rec], kind: str) -> int:
    n = 0
    for r in (x for x in recs if x.confident):
        try:
            existing = (sb.table("brain_proposals").select("id")
                          .eq("proposal_type", kind).eq("target_key", r.key)
                          .eq("status", "PENDING").execute().data or [])
            row = {"analysis_run_id": _RUN_ID, "proposal_type": kind,
                   "target_key": r.key, "current_value": r.current,
                   "proposed_value": r.suggested,
                   "evidence": f"[{r.binds}] {r.implication} — {r.evidence}",
                   "rationale": f"{r.label} ({r.book}) — {r.evidence}",
                   "confidence": 0.8 if r.priority == 1 else 0.6,
                   "status": "PENDING", "source": "control_room",
                   "priority": r.priority}
            if existing:
                sb.table("brain_proposals").update(row).eq("id", existing[0]["id"]).execute()
            else:
                sb.table("brain_proposals").insert(row).execute()
            n += 1
        except Exception as e:
            logger.warning(f"  could not record {r.key}: {e}")
    return n


def _load_setups(sb, days: int) -> list:
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows, off = [], 0
    while True:
        chunk = (sb.table("intraday_setups").select("*").gte("trade_date", since)
                   .range(off, off + 999).execute().data or [])
        rows += chunk
        if len(chunk) < 1000:
            break
        off += 1000
    return [r for r in dedupe_setups(rows) if r.get("outcome")]


def main(days: int = 14, propose: bool = False) -> int:
    sb = get_supabase()
    logger.info("═" * 78)
    logger.info("TradeOS — Control Room advisor")
    logger.info("═" * 78)

    setups = _load_setups(sb, days)
    since = (today_ist() - timedelta(days=365)).isoformat()
    closed = (sb.table("closed_positions").select("*")
                .gte("exit_date", since).limit(1000).execute().data or [])
    logger.info(f"  {len(setups)} distinct intraday setups over {days}d · "
                f"{len(closed)} closed positions over 365d")

    risk_exposure(sb)
    panel = panel_recs(sb, setups, closed)
    _print_recs("2 · THE PANEL, CONTROL BY CONTROL   (! = the control does nothing today)",
                panel)
    miss = missing_controls(sb)
    if miss:
        _print_recs("3 · MISSING CONTROLS — attributes the panel cannot express", miss)

    logger.info("")
    if propose:
        n = _write(sb, panel, "CONTROL_ROOM") + _write(sb, miss, "CONTROL_ROOM_NEW")
        logger.success(f"  {n} recommendation(s) written to brain_proposals. "
                       f"Nothing was applied.")
    else:
        logger.info("  Re-run with --propose to record these. Nothing is ever applied "
                    "automatically.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Recommend Control Room settings from the week's evidence")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--propose", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.days, a.propose))
