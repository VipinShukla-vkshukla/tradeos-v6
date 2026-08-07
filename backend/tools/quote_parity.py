"""
Do the websocket and the historical endpoint agree, before either is trusted?

    python -m tools.quote_parity            report the divergence collected
    python -m tools.quote_parity --arm      turn logging on for one session
    python -m tools.quote_parity --disarm   turn it off again

WHY PARITY BEFORE PROMOTION
---------------------------
Stage 5 replaces a 300-second-old day range with a live one. That is only an
upgrade if the live number is right. A feed that disagrees with the historical
endpoint about today's high is not a faster truth — it is a new bug with better
latency, and it would be wired directly into the breakout condition of every
opening-range engine.

So the two sources are logged side by side for one session and compared here.
The two switches that consume them, `intraday_quote_mode_range` (day_open/
day_high/day_low/volume) and `intraday_quote_mode_vwap` (vwap), stay off
until this reports agreement for their own fields — 08-Aug-2026, measured
independently after the two groups turned out to disagree (range clean,
vwap FAULT). `prev_close` has no switch at all: it is time-invariant
intraday, so a live overlay is never the fix for it regardless of what this
reports — see intraday/engine.py::apply_live_quotes()'s docstring.

WHAT COUNTS AS AGREEMENT
------------------------
Not "the means are close". Day high and day low are RATCHETS — they only ever
move one way — so the live value may legitimately lead the fetched one, and a
one-sided lead is expected rather than alarming. What is not expected is the
live value being BEHIND, or the two disagreeing in both directions, which is
what a wrong instrument mapping or a stale cache looks like.

VOLUME IS EXCLUDED FROM THE VERDICT.
The websocket reports cumulative traded volume for the session; the historical
endpoint reports the sum of completed bars. They measure different things and
will never match, so scoring them together would fail a parity check that is
actually passing. It is logged for visibility and reported separately.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


# Fields whose disagreement would change a decision. Volume is logged but not
# scored — see the module docstring.
SCORED = ("day_high", "day_low", "vwap", "prev_close")

# Ratchets: the live value leading the fetched one is correct behaviour, not
# divergence. Only a live value BEHIND the fetched one is a fault.
RATCHET_UP   = ("day_high",)
RATCHET_DOWN = ("day_low",)


def compare(symbol: str, field: str, live, fetched) -> dict | None:
    """
    Build one comparison row, or None when there is nothing to compare.

    Pure — no I/O — so the caller can collect a whole cycle's worth and write
    them in ONE round trip. The first version inserted per symbol per field
    inside the 15-second decision loop, which is ~91,000 synchronous writes a
    session sitting in front of exit evaluation on live positions.
    """
    try:
        if live is None or fetched is None or not float(fetched):
            return None
        live, fetched = float(live), float(fetched)
    except (TypeError, ValueError):
        return None
    return {"symbol": symbol, "field": field,
            "live_value": live, "fetched_value": fetched,
            "diff_pct": round((live - fetched) / fetched * 100.0, 4)}


def record_many(sb, rows: list[dict]) -> int:
    """
    Write a batch. Deliberately tolerant: parity logging must never be able to
    break a session it is only observing.
    """
    if not rows:
        return 0
    try:
        sb.table("intraday_quote_parity").insert(rows).execute()
        return len(rows)
    except Exception:
        return 0


def record(sb, symbol: str, field: str, live, fetched) -> None:
    """Single-row convenience, kept for callers outside the hot path."""
    record_many(sb, [r for r in (compare(symbol, field, live, fetched),) if r])


def report(sb) -> int:
    rows, off = [], 0
    while True:
        page = (sb.table("intraday_quote_parity")
                  .select("symbol,field,live_value,fetched_value,diff_pct,ts")
                  .range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000

    if not rows:
        logger.error("  no parity rows collected")
        logger.info("  arm it, run one session, then come back:")
        logger.info("    python -m tools.quote_parity --arm")
        return 1

    by = defaultdict(list)
    for r in rows:
        if r.get("diff_pct") is not None:
            by[r["field"]].append(float(r["diff_pct"]))

    span = f"{min(str(r['ts']) for r in rows)[:16]} to {max(str(r['ts']) for r in rows)[:16]}"
    logger.info("=" * 74)
    logger.info("QUOTE PARITY - websocket against the historical endpoint")
    logger.info("=" * 74)
    logger.info(f"  {len(rows)} comparison(s), {len({r['symbol'] for r in rows})} symbol(s), {span}")
    logger.info("")
    logger.info(f"  {'field':<12} {'n':>5} {'median':>9} {'mean':>9} {'worst':>9}  verdict")

    field_ok: dict[str, bool] = {}
    for field in sorted(by):
        d = by[field]
        med, mean = statistics.median(d), statistics.fmean(d)
        worst = max(d, key=abs)

        if field not in SCORED:
            note = "not scored (measures a different quantity)"
        elif field in RATCHET_UP:
            # A live high BELOW the fetched high means the socket missed a print.
            behind = [x for x in d if x < -0.05]
            note = (f"OK ({len(behind)} behind)" if not behind
                    else f"FAULT - live high behind on {len(behind)} of {len(d)}")
            field_ok[field] = not behind
        elif field in RATCHET_DOWN:
            behind = [x for x in d if x > 0.05]
            note = (f"OK ({len(behind)} behind)" if not behind
                    else f"FAULT - live low behind on {len(behind)} of {len(d)}")
            field_ok[field] = not behind
        else:
            bad = [x for x in d if abs(x) > 0.10]
            note = "OK" if not bad else f"FAULT - {len(bad)} of {len(d)} beyond 0.10%"
            field_ok[field] = not bad

        log = logger.info if note.startswith(("OK", "not scored")) else logger.error
        log(f"  {field:<12} {len(d):>5} {med:>8.3f}% {mean:>8.3f}% {worst:>8.3f}%  {note}")

    # Reported per switch, not as one blanket verdict — intraday_quote_mode_range
    # and intraday_quote_mode_vwap are independently gated (08-Aug-2026) because
    # the two groups measured differently: day_high/day_low held clean while
    # vwap FAULTed. prev_close has no switch at all — see the module docstring.
    logger.info("")
    range_fields = [f for f in ("day_high", "day_low") if f in field_ok]
    range_ok = all(field_ok[f] for f in range_fields) if range_fields else None
    if range_ok:
        logger.success("  RANGE (day_high/day_low) HOLDS. Safe to enable:")
        logger.success("    UPDATE system_config SET value='true' "
                       "WHERE key='intraday_quote_mode_range';")
    elif range_ok is False:
        logger.error("  RANGE (day_high/day_low) FAILS. Leave intraday_quote_mode_range off.")
    else:
        logger.info("  RANGE (day_high/day_low) — no data collected yet.")

    vwap_ok = field_ok.get("vwap")
    if vwap_ok:
        logger.success("  VWAP HOLDS. Safe to enable:")
        logger.success("    UPDATE system_config SET value='true' "
                       "WHERE key='intraday_quote_mode_vwap';")
    elif vwap_ok is False:
        logger.error("  VWAP FAILS. Leave intraday_quote_mode_vwap off — this is a formula "
                     "disagreement (tick VWAP vs bar-approximation VWAP), not just staleness; "
                     "needs a reconciled definition, not a resync.")
    else:
        logger.info("  VWAP — no data collected yet.")

    if "prev_close" in field_ok and not field_ok["prev_close"]:
        logger.error("  PREV_CLOSE FAULTs, but has no switch — it is never live-overlaid "
                     "regardless. The fix is in refresh_contexts()'s stock_data_daily "
                     "lookup, not here.")

    logger.info("")
    if range_ok or vwap_ok:
        logger.info("  Once whichever switch(es) you enable have run a session, disarm "
                    "the logging — it is meant for one session, not forever:")
        logger.info("    python -m tools.quote_parity --disarm")
    return 0 if (range_ok is not False and vwap_ok is not False) else 1


def _set(sb, on: bool) -> int:
    sb.table("system_config").update({"value": "true" if on else "false"}) \
      .eq("key", "intraday_quote_parity_log").execute()
    logger.success(f"  parity logging {'ARMED' if on else 'disarmed'}")
    if on:
        logger.info("  run one full session, then: python -m tools.quote_parity")
        logger.info("  the daemon picks this up within 300s — it re-reads config on "
                    "its slow timer")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Websocket vs historical endpoint parity")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--arm", action="store_true", help="turn parity logging on")
    g.add_argument("--disarm", action="store_true", help="turn parity logging off")
    a = ap.parse_args(argv)

    from config import get_supabase
    sb = get_supabase()
    if a.arm:
        return _set(sb, True)
    if a.disarm:
        return _set(sb, False)
    return report(sb)


if __name__ == "__main__":
    sys.exit(main())
