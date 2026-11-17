"""
TradeOS v7 — Capital Reconciliation
====================================
Compares the TOTAL_CAPITAL the system SIZES ON against what the broker account
actually holds.

WHY THIS EXISTS
---------------
Position sizing runs off a single number, TOTAL_CAPITAL, read from .env or a
GitHub secret. Nothing has ever checked it against reality. On 2026-07-27 the
configured figure was Rs 20,000 while Kite reported Rs 5,200 available cash and
Rs 8,644 invested — Rs 13,844 in total. Every new entry was therefore sized
against roughly Rs 6,000 of headroom that does not exist, and the first symptom
would have been a rejected order at the broker, mid-session, on a signal the
system had already told you to take.

That is a silent, compounding error: it never fails a check, it just quietly
proposes trades slightly too large, and the gap widens every time capital moves
without .env being updated.

DELIBERATELY A FLAG, NOT A CORRECTION
-------------------------------------
This module does NOT overwrite TOTAL_CAPITAL. A shortfall can mean two opposite
things — "I have not funded the account yet" or "I withdrew and forgot to update
the config" — and only the operator knows which. Auto-correcting downward would
also silently shrink every position the moment cash was temporarily parked
elsewhere. So it reports, loudly and with both numbers, and leaves the decision
where it belongs.

The snapshot is persisted to system_config so the dashboard can render the same
comparison without needing its own broker session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime
from loguru import logger
from config import get_supabase, TOTAL_CAPITAL, CAPITAL_IS_FALLBACK, IST, cfg_float

SNAPSHOT_KEY = "capital_snapshot"


def capital_status(sb=None, persist: bool = True) -> dict:
    """
    {configured, broker_cash, broker_invested, broker_total, gap, gap_pct,
     severity, message, as_of}

    severity is OK | WARN | BLOCK | UNKNOWN. Never raises — a broker hiccup must
    not take down the caller, and "we could not check" is reported as exactly
    that rather than as agreement.
    """
    sb = sb or get_supabase()
    now = datetime.now(IST).isoformat()

    import socket
    out = {
        "configured":      round(TOTAL_CAPITAL, 2),
        "is_fallback":     CAPITAL_IS_FALLBACK,
        # WHICH MACHINE believes this. TOTAL_CAPITAL comes from a per-machine
        # .env, but it describes the ACCOUNT — one book, one number. Two hosts
        # manage this account and on 2026-08-06 they disagreed: the laptop
        # sized against ₹30,000 and the server against ₹20,000, so every
        # position size depended on which daemon happened to hold the lease,
        # and nothing anywhere compared the two. Recording the author is what
        # makes the disagreement detectable at all.
        "hostname":        socket.gethostname(),
        "broker_cash":     None,
        "broker_invested": None,
        "broker_total":    None,
        "gap":             None,
        "gap_pct":         None,
        "severity":        "UNKNOWN",
        "message":         "",
        "as_of":           now,
    }

    try:
        from kite import kite_client
        if not kite_client.is_available():
            out["message"] = "No Kite session — capital could not be verified against the broker."
            _persist(sb, out, persist)
            return out

        margins  = kite_client.fetch_margins() or {}
        holdings = kite_client.fetch_holdings() or []
    except Exception as e:
        out["message"] = f"Broker query failed: {e}"
        _persist(sb, out, persist)
        return out

    cash = float(margins.get("available_cash") or 0)
    # Value holdings at LAST TRADED price, not at cost. Deployable capital is
    # what the book is worth now — sizing against cost basis would understate a
    # winning book and overstate a losing one.
    invested = 0.0
    for h in holdings:
        qty = float(h.get("quantity") or 0) + float(h.get("t1_quantity") or 0)
        px  = float(h.get("last_price") or h.get("average_price") or 0)
        invested += qty * px

    total = cash + invested
    gap     = total - TOTAL_CAPITAL
    gap_pct = (gap / TOTAL_CAPITAL * 100) if TOTAL_CAPITAL else 0.0

    out.update({
        "broker_cash":     round(cash, 2),
        "broker_invested": round(invested, 2),
        "broker_total":    round(total, 2),
        "gap":             round(gap, 2),
        "gap_pct":         round(gap_pct, 2),
    })

    # Tolerance, because intraday marks move and a rupee-exact match is noise.
    tol_pct = cfg_float("capital_tolerance_pct", 10.0)
    block_pct = cfg_float("capital_block_pct", 25.0)

    if abs(gap_pct) <= tol_pct:
        out["severity"] = "OK"
        out["message"]  = (f"Configured Rs {TOTAL_CAPITAL:,.0f} vs broker "
                           f"Rs {total:,.0f} — within {tol_pct:.0f}%.")
    elif gap < 0:
        # The dangerous direction: sizing against money that is not there.
        out["severity"] = "BLOCK" if abs(gap_pct) > block_pct else "WARN"
        out["message"]  = (
            f"TOTAL_CAPITAL is Rs {TOTAL_CAPITAL:,.0f} but the account holds "
            f"Rs {total:,.0f} (Rs {cash:,.0f} cash + Rs {invested:,.0f} invested) "
            f"— short by Rs {abs(gap):,.0f} ({abs(gap_pct):.0f}%). New entries are "
            f"being sized against headroom that does not exist; expect orders to "
            f"be rejected at the broker. Set TOTAL_CAPITAL to the real figure or "
            f"fund the account."
        )
    else:
        # Harmless but worth saying — you are under-deploying.
        out["severity"] = "WARN"
        out["message"]  = (
            f"The account holds Rs {total:,.0f} but TOTAL_CAPITAL is set to "
            f"Rs {TOTAL_CAPITAL:,.0f} — Rs {gap:,.0f} ({gap_pct:.0f}%) is not being "
            f"sized against. Raise TOTAL_CAPITAL to deploy it."
        )

    if CAPITAL_IS_FALLBACK:
        out["message"] += (" Note: TOTAL_CAPITAL is unset, so this comparison is "
                           "against the built-in fallback, not a figure you chose.")

    _persist(sb, out, persist)
    return out


def _persist(sb, payload: dict, enabled: bool) -> None:
    """Store for the dashboard, which has no broker session of its own."""
    if not enabled:
        return
    try:
        row = {"key": SNAPSHOT_KEY, "value": json.dumps(payload),
               "updated_at": datetime.now(IST).isoformat()}
        existing = sb.table("system_config").select("key").eq("key", SNAPSHOT_KEY).execute().data
        if existing:
            sb.table("system_config").update(
                {"value": row["value"], "updated_at": row["updated_at"]}
            ).eq("key", SNAPSHOT_KEY).execute()
        else:
            sb.table("system_config").insert({
                **row,
                "description": "Configured vs broker capital — written by control/capital_check.py",
            }).execute()
    except Exception as e:
        logger.debug(f"  capital snapshot not persisted: {e}")


def log_capital_status(sb=None) -> dict:
    """Run the check and log it at the severity it deserves."""
    st = capital_status(sb)
    msg = f"  Capital: {st['message']}"
    if st["severity"] == "BLOCK":
        logger.error(msg)
    elif st["severity"] == "WARN":
        logger.warning(msg)
    elif st["severity"] == "OK":
        logger.info(msg)
    else:
        logger.debug(msg)
    return st


if __name__ == "__main__":
    import pprint
    pprint.pprint(log_capital_status())
