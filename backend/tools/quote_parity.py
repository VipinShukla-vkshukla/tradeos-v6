"""
Do the websocket and the historical endpoint agree, before either is trusted
— and do they STILL agree, for as long as anything is acting on it?

    python -m tools.quote_parity            report the divergence collected
    python -m tools.quote_parity --arm      turn logging on
    python -m tools.quote_parity --disarm   turn it off

WHY PARITY BEFORE PROMOTION
---------------------------
Stage 5 replaces a 300-second-old day range with a live one. That is only an
upgrade if the live number is right. A feed that disagrees with the historical
endpoint about today's high is not a faster truth — it is a new bug with better
latency, and it would be wired directly into the breakout condition of every
opening-range engine.

So the two sources are logged side by side and compared here.

ONE SESSION IS EVIDENCE, NOT A VERDICT — 08-Aug-2026. The first version of this
told the operator to arm parity, run one session, and disarm once it read
clean — a permanent decision frozen on whatever that one session's market did.
A quiet range-bound Thursday and a fast Monday do not necessarily produce the
same agreement, and nothing was left running to notice if they didn't. Parity
logging is cheap (rate-limited to once per 300s per symbol, one batched write
— see apply_live_quotes()), so there is no real reason to ever turn it off
while either switch below is on. Recommended posture now: arm it, and leave
it armed for as long as either switch is on. `tools.health`'s `quote_parity`
check enforces this — it fails if a switch is on but logging is off, or if
recent data has started disagreeing, so a regression on any later session
gets caught by the check you are told to run before trading, not
rediscovered by hand or, worse, not discovered at all.

Two independent switches consume this: `intraday_quote_mode_range` (day_open/
day_high/day_low/volume, measured clean 07-Aug-2026, recommended ON) and
`intraday_quote_mode_vwap` (vwap, measured FAULT against the tolerances the
engines that read it actually use — see `_vwap_engine_relevance` below,
recommended OFF and not expected to flip soon, since the fault traces to a
structural formula difference rather than staleness). Both stay off by
default; both are cheap to leave in place either way, which is the point —
revisiting either is a config flip once fresh data supports it, not a code
change. `prev_close` has no switch at all, not "off by default" but no
overlay code path — it is time-invariant intraday, so a live overlay is
never the fix for it regardless of what this reports. All three fields stay
measured here for visibility — see
intraday/engine.py::apply_live_quotes()'s docstring.

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

# The blanket 0.10% parity band answers "do the two sources roughly agree",
# not "does this matter to the engines that read vwap". Those engines gate on
# distance from vwap directly, at tolerances of their own — some tighter than
# 0.10%. This is the question that actually decides whether
# intraday_quote_mode_vwap is safe: of the comparisons collected, how many
# would have moved a real engine's boundary by more than its own precision
# requires. (config key, engine file, what it gates)
VWAP_ENGINE_TOLERANCES = (
    ("vwr_stop_buffer_pct",         0.08, "vwap_reclaim.py — stop distance below swing low"),
    ("intraday_short_stop_buffer_pct", 0.12, "short_distribution.py — stop distance above vwap"),
    ("pbk_stop_buffer_pct",         0.15, "pullback.py — stop distance below vwap"),
    ("pbk_touch_tol_pct",           0.25, "pullback.py — 'touched vwap' band"),
    ("intraday_short_vwap_near_pct", 0.35, "short_distribution.py — 'near vwap' band"),
    ("vwr_max_extension_pct",       0.45, "vwap_reclaim.py — max extension past vwap gate"),
)


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


def range_verdict(rows: list[dict]) -> tuple[bool | None, str]:
    """
    OK/FAULT for day_high/day_low ONLY — the two fields intraday_quote_mode_range
    actually gates. Pure, no I/O, so it is usable both by report() (the full
    CLI breakdown of all fields) and by tools.health.check_quote_parity_freshness
    (the ongoing check that quote mode, once enabled, keeps being re-verified
    against CURRENT data rather than trusted forever on one session's numbers).
    One ratchet definition, in one place, read by both.

    None means no day_high/day_low rows were in what was passed in — a
    distinct case from "verified clean", since the caller decides what that
    means (report() says "no data yet"; health.py fails loudly, because ON
    with nothing to verify against is not the same as verified).
    """
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        f = r.get("field")
        if f in ("day_high", "day_low") and r.get("diff_pct") is not None:
            by[f].append(float(r["diff_pct"]))
    if not by:
        return None, "no day_high/day_low comparisons in range"

    bad = []
    if "day_high" in by:
        bad += [x for x in by["day_high"] if x < -0.05]
    if "day_low" in by:
        bad += [x for x in by["day_low"] if x > 0.05]
    n = sum(len(v) for v in by.values())
    if bad:
        return False, f"{len(bad)} of {n} day_high/day_low comparisons behind"
    return True, f"{n} day_high/day_low comparisons, all clean"


def vwap_verdict(rows: list[dict]) -> tuple[bool | None, str]:
    """
    OK/FAULT for vwap against the TIGHTEST tolerance any engine that reads it
    actually uses (VWAP_ENGINE_TOLERANCES), read live so an operator override
    is respected — not the blanket 0.10% band, which answers a different
    question. Same reuse rationale as range_verdict: one definition, shared by
    report()'s detailed CLI breakdown (via _vwap_engine_relevance) and
    tools.health's ongoing check, so intraday_quote_mode_vwap is held to the
    same "verify against CURRENT data, not last week's" standard as range —
    it just isn't expected to pass any time soon, for a structural reason
    rather than a staleness one.
    """
    from config import cfg_float
    diffs = [float(r["diff_pct"]) for r in rows
             if r.get("field") == "vwap" and r.get("diff_pct") is not None]
    if not diffs:
        return None, "no vwap comparisons in range"
    tightest = min(cfg_float(key, default) for key, default, _ in VWAP_ENGINE_TOLERANCES)
    over = [x for x in diffs if abs(x) >= tightest]
    n = len(diffs)
    if over:
        return False, (f"{len(over)} of {n} vwap comparisons crossed the tightest "
                       f"engine tolerance ({tightest:.2f}%)")
    return True, f"{n} vwap comparisons, all inside every engine's tolerance"


def _vwap_engine_relevance(diffs: list[float]) -> None:
    """
    The 0.10% parity band is a generic "do these roughly agree" test. It does
    not answer whether a disagreement is big enough to flip a decision an
    engine actually makes — that depends on each engine's own tolerance, which
    is sometimes tighter than 0.10%. This reports, for each one, how many of
    the collected comparisons exceeded it — read live so an operator override
    is respected rather than the hardcoded default silently going stale.
    """
    from config import cfg_float
    logger.info("")
    logger.info("  vwap vs the engines that actually gate on it "
               "(live tolerance, not the blanket 0.10% band above):")
    for key, default, where in VWAP_ENGINE_TOLERANCES:
        tol = cfg_float(key, default)
        over = [x for x in diffs if abs(x) >= tol]
        pct = len(over) / len(diffs) * 100.0 if diffs else 0.0
        note = "clear" if not over else f"{len(over)} of {len(diffs)} ({pct:.1f}%) could flip this"
        log = logger.info if not over else logger.warning
        log(f"    {key:<32} {tol:>5.2f}%  {where:<48} {note}")


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

    if "vwap" in by:
        _vwap_engine_relevance(by["vwap"])

    # Reported per switch via range_verdict/vwap_verdict — the SAME functions
    # tools.health's ongoing check calls, so a fresh re-run of this command
    # and the check that runs before every trading session agree by
    # construction, not by two copies of the same logic staying in sync.
    logger.info("")
    range_ok, range_detail = range_verdict(rows)
    if range_ok:
        logger.success(f"  RANGE: {range_detail}. Safe to enable — on THIS data:")
        logger.success("    UPDATE system_config SET value='true' "
                       "WHERE key='intraday_quote_mode_range';")
        logger.info("    (keep parity logging armed after — see the module docstring)")
    elif range_ok is False:
        logger.error(f"  RANGE: {range_detail}. Leave intraday_quote_mode_range off.")
    else:
        logger.info(f"  RANGE: {range_detail}.")

    vwap_ok, vwap_detail = vwap_verdict(rows)
    if vwap_ok:
        logger.success(f"  VWAP: {vwap_detail}. Safe to enable — on THIS data:")
        logger.success("    UPDATE system_config SET value='true' "
                       "WHERE key='intraday_quote_mode_vwap';")
    elif vwap_ok is False:
        logger.error(f"  VWAP: {vwap_detail}. Leave intraday_quote_mode_vwap off — this is "
                     "a formula disagreement (tick VWAP vs bar-approximation VWAP), not "
                     "staleness, so it is unlikely to clear on its own with more data.")
    else:
        logger.info(f"  VWAP: {vwap_detail}.")

    if "prev_close" in field_ok and not field_ok["prev_close"]:
        logger.error("  PREV_CLOSE FAULTs, but has no switch — it is never live-overlaid "
                     "regardless. The fix is in refresh_contexts()'s stock_data_daily "
                     "lookup, not here.")

    logger.info("")
    logger.info("  Whichever switch(es) are on, keep parity logging armed — this is a "
                "point-in-time read of whatever data exists right now, not a permanent "
                "verdict. tools.health re-checks it before every trading session.")
    return 0 if (range_ok is not False and vwap_ok is not False) else 1


def _set(sb, on: bool) -> int:
    sb.table("system_config").update({"value": "true" if on else "false"}) \
      .eq("key", "intraday_quote_parity_log").execute()
    logger.success(f"  parity logging {'ARMED' if on else 'disarmed'}")
    if on:
        logger.info("  after a session or two: python -m tools.quote_parity")
        logger.info("  the daemon picks this up within 300s — it re-reads config on "
                    "its slow timer")
        logger.info("  leave this armed for as long as intraday_quote_mode_range and/or "
                    "_vwap might be on — it is cheap, and tools.health checks that it "
                    "stayed armed")
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
