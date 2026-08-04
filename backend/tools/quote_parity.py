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
The switch that consumes them, `intraday_quote_mode`, stays off until this
reports agreement.

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


def record(sb, symbol: str, field: str, live, fetched) -> None:
    """
    Write one comparison. Called from the slow timer when logging is armed.

    Deliberately tolerant: parity logging must never be able to break a session
    it is only observing.
    """
    try:
        if live is None or fetched is None or not float(fetched):
            return
        live, fetched = float(live), float(fetched)
        sb.table("intraday_quote_parity").insert({
            "symbol": symbol, "field": field,
            "live_value": live, "fetched_value": fetched,
            "diff_pct": round((live - fetched) / fetched * 100.0, 4),
        }).execute()
    except Exception:
        pass


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

    verdict_ok = True
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
            verdict_ok &= not behind
        elif field in RATCHET_DOWN:
            behind = [x for x in d if x > 0.05]
            note = (f"OK ({len(behind)} behind)" if not behind
                    else f"FAULT - live low behind on {len(behind)} of {len(d)}")
            verdict_ok &= not behind
        else:
            bad = [x for x in d if abs(x) > 0.10]
            note = "OK" if not bad else f"FAULT - {len(bad)} of {len(d)} beyond 0.10%"
            verdict_ok &= not bad

        log = logger.info if note.startswith(("OK", "not scored")) else logger.error
        log(f"  {field:<12} {len(d):>5} {med:>8.3f}% {mean:>8.3f}% {worst:>8.3f}%  {note}")

    logger.info("")
    if verdict_ok:
        logger.success("  PARITY HOLDS. Quote mode is safe to enable:")
        logger.success("    UPDATE system_config SET value='true' WHERE key='intraday_quote_mode';")
        logger.info("  Then disarm the logging — it is meant for one session, not forever:")
        logger.info("    python -m tools.quote_parity --disarm")
        return 0
    logger.error("  PARITY FAILS. Leave intraday_quote_mode off.")
    logger.error("  A live field that disagrees with the fetched one would be wired "
                 "straight into every opening-range breakout condition.")
    return 1


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
