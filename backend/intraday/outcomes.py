"""
Did the setup work? — the intraday learning loop.

WHY THIS MATTERS MORE HERE THAN IN SWING
-----------------------------------------
The swing framework has been running for months and has 70 closed trades, of
which exactly ONE is attributed to a signal. That is why its ML model is
untrainable and why no engine's lifecycle state can be justified with evidence.
The intraday subsystem produces setups at ten times the rate, so the same
mistake would compound ten times faster.

So outcomes are resolved from the start, for every setup DETECTED — including
the ones rejected on cost. "ORB fired 40 times and 12 would have worked" is a
different and far more useful statement than "we took 3 ORB trades and 1 worked",
and only the first can justify enabling, shadowing or retiring an engine.

HOW AN OUTCOME IS DECIDED
-------------------------
Replay the session's bars after the setup was created and ask which came first:

    TARGET   the target traded before the stop            -> win
    STOP     the stop traded before the target            -> loss
    TIMEOUT  neither, and the session ended               -> scratch, measured
             at the close, because an intraday setup that
             resolves nothing IS a small loss after costs

The pessimistic tie-break matters: when a single bar spans both the stop and
the target, this records a STOP. Intraday bars are coarse enough that both are
genuinely possible, and a backtest that assumes the good one is how a strategy
looks profitable on paper and loses money live.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date, datetime, timedelta
from loguru import logger
from config import IST, get_supabase, cfg, today_ist
from intraday import direction as D


def _session_bars(kite, token: int, day: date, interval: str) -> list:
    """
    Every bar for one symbol on one day. ONE broker call, cached by the caller.

    This used to be `_bars_after`, called once per SETUP. A session routinely
    produces several setups on the same symbol — 236 detections over 13 symbols
    on 28 July — so resolving that day meant 236 rate-limited historical_data
    calls to fetch the same 13 series over and over. Kite allows about three a
    second, so the replay took minutes and was the most likely thing to fail
    part-way and leave the day half-scored.

    Fetch once per symbol, filter per setup in memory.
    """
    try:
        raw = kite.historical_data(token, day, day, interval) or []
    except Exception as e:
        logger.debug(f"  bars unavailable for token {token} on {day}: {e}")
        return []
    out = []
    for b in raw:
        ts = b["date"]
        if ts.tzinfo is None:
            ts = IST.localize(ts)
        out.append({**b, "date": ts})
    return out


def resolve_day(trade_date: str | None = None, sb=None) -> dict:
    """
    Resolve every unresolved setup for a date. Idempotent.

    Run after the close — from the evening pipeline, or by hand. Setups already
    carrying an outcome are skipped, so re-running is free.
    """
    sb = sb or get_supabase()
    d = trade_date or today_ist().isoformat()

    rows = (sb.table("intraday_setups").select("*")
              .eq("trade_date", d).is_("outcome", "null")
              .execute().data or [])
    if not rows:
        logger.info(f"  outcomes: nothing unresolved for {d}")
        return {"resolved": 0}

    try:
        from kite import kite_client
        kite = kite_client.get_kite()
        if not kite:
            logger.warning("  outcomes: no broker session — cannot replay bars")
            return {"resolved": 0, "reason": "no_broker"}
    except Exception as e:
        logger.warning(f"  outcomes: broker unavailable — {e}")
        return {"resolved": 0, "reason": "no_broker"}

    interval = cfg("intraday_bar_interval", "minute")
    day = datetime.strptime(d, "%Y-%m-%d").date()
    symbols = sorted({r["symbol"] for r in rows})

    try:
        tokens = kite.ltp([f"NSE:{s}" for s in symbols])
        tok = {k.split(":", 1)[1]: v["instrument_token"] for k, v in (tokens or {}).items()}
    except Exception as e:
        logger.warning(f"  outcomes: token lookup failed — {e}")
        return {"resolved": 0}

    tally = {"TARGET": 0, "STOP": 0, "TIMEOUT": 0, "UNKNOWN": 0}
    resolved = 0
    bar_cache: dict[str, list] = {}

    for r in rows:
        sym = r["symbol"]
        t = tok.get(sym)
        if not t:
            continue
        created = r.get("ts")
        try:
            after = datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(IST)
        except Exception:
            after = IST.localize(datetime.combine(day, datetime.min.time()))

        if sym not in bar_cache:
            bar_cache[sym] = _session_bars(kite, t, day, interval)
        bars = [b for b in bar_cache[sym] if b["date"] >= after]
        if not bars:
            tally["UNKNOWN"] += 1
            continue

        entry = float(r.get("entry") or 0)
        stop  = float(r.get("stop") or 0)
        tgt   = float(r.get("target") or 0)
        if not (entry and stop and tgt):
            tally["UNKNOWN"] += 1
            continue

        # DIRECTION DECIDES WHICH EXTREME OF THE BAR HITS WHICH LEVEL.
        #
        # `lo <= stop, hi >= tgt` is the long form, and on a short it is not
        # merely wrong — it is wrong on the FIRST BAR, every time. A short's stop
        # sits ABOVE its entry, so `lo <= stop` is true immediately and every
        # short would have resolved STOP at its own stop price within seconds of
        # detection. The learning loop reads this table: `intraday_priors` and
        # `hurdle`'s arrival distribution are both built from it, so a short
        # engine would have been assigned a catastrophic measured prior made
        # entirely of an arithmetic error, and then retired on that evidence.
        d = D.normalise(r.get("direction"))
        outcome, exit_px = "TIMEOUT", float(bars[-1]["close"])
        for b in bars:
            hi, lo = float(b["high"]), float(b["low"])
            if D.is_short(d):
                hit_stop, hit_tgt = hi >= stop, lo <= tgt
            else:
                hit_stop, hit_tgt = lo <= stop, hi >= tgt
            if hit_stop and hit_tgt:
                # Both inside one bar. Assume the bad one — a coarse bar cannot
                # tell you the sequence, and assuming the good one is how a
                # strategy looks profitable on paper and loses money live.
                outcome, exit_px = "STOP", stop
                break
            if hit_stop:
                outcome, exit_px = "STOP", stop
                break
            if hit_tgt:
                outcome, exit_px = "TARGET", tgt
                break

        # Signed IN THE TRADE'S FAVOUR, not in the price's direction. A short
        # that fell 1% made +1%. Unsigned, every profitable short would have been
        # recorded as a loss of the same size, which is the sign error that
        # turns a working engine into a retired one.
        pct = D.gain_pct(entry, exit_px, d)
        # Net of the round trip, because a gross win under 0.21% is a loss and
        # recording it as a win would teach the wrong lesson.
        cost = float(r.get("cost_pct") or 0)
        try:
            sb.table("intraday_setups").update({
                "outcome": outcome,
                "outcome_pct": round(pct - cost, 3),
            }).eq("id", r["id"]).execute()
            tally[outcome] += 1
            resolved += 1
        except Exception as e:
            logger.debug(f"  outcomes: update failed for {sym}: {e}")

    logger.success(
        f"  outcomes {d}: {resolved} resolved from {len(bar_cache)} symbol "
        f"fetch(es) — {tally['TARGET']} target, {tally['STOP']} stop, "
        f"{tally['TIMEOUT']} timeout"
        + (f", {tally['UNKNOWN']} unknown" if tally["UNKNOWN"] else "")
    )
    return {"resolved": resolved, **tally}


def unresolved_days(sb=None, days: int = 30) -> list[tuple[str, int]]:
    """
    Past sessions that still carry unscored detections. Newest first.

    Today is excluded: its setups are legitimately unresolved until the close,
    and reporting them as a gap would make the health check cry wolf every
    single trading day — which is how a real warning stops being read.
    """
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()
    today = today_ist().isoformat()
    rows = (sb.table("intraday_setups").select("trade_date")
              .gte("trade_date", since).is_("outcome", "null")
              .execute().data or [])
    tally: dict[str, int] = {}
    for r in rows:
        d = str(r.get("trade_date"))
        if d and d != today:
            tally[d] = tally.get(d, 0) + 1
    return sorted(tally.items(), reverse=True)


def backfill(days: int = 30, sb=None) -> dict:
    """
    Resolve every past session that was left unscored. Idempotent.

    WHY THIS HAS TO EXIST SEPARATELY FROM resolve_day
    -------------------------------------------------
    resolve_day() defaults to TODAY and is called from exactly one place: the
    `finally` block of the intraday daemon, and only when that daemon held the
    lease. So a day is scored if and only if the daemon started, acquired the
    lease, and exited cleanly on that specific day. Miss any of those — a crash,
    a hard kill, a laptop lid, the lease held by the other machine — and the
    day is skipped, and nothing ever revisits it. The setups sit with a
    cost_verdict and a NULL outcome forever.

    That is not hypothetical. On 31 July the table held 460 detections and 241
    of them — all of 28 and 29 July — were unresolved and unreachable, because
    resolve_day had only ever run for the day it was invoked on. The engines
    that fire in the first hour are hit hardest, since a daemon started late
    misses the whole ORB and GAP window and then never scores it.

    The cost of that is not visible anywhere. weekly_review needs 20 resolved
    outcomes before it will judge an engine at all, so a starved loop does not
    report a problem — it reports "only 8 outcomes, below the 20 needed", which
    reads like patience rather than breakage. This is the check that makes the
    absence visible, and tools/health.py fails on it.
    """
    sb = sb or get_supabase()
    pending = unresolved_days(sb, days)
    if not pending:
        logger.success("  outcomes: no unresolved past sessions")
        return {"days": 0, "resolved": 0}

    logger.warning(f"  outcomes: {len(pending)} past session(s) never scored — "
                   f"{sum(n for _, n in pending)} detection(s) the learning loop "
                   f"has never seen")
    total = 0
    for d, n in pending:
        logger.info(f"    backfilling {d} ({n} unresolved)")
        try:
            total += resolve_day(d, sb=sb).get("resolved", 0)
        except Exception as e:
            logger.warning(f"    {d} could not be backfilled — {e}")
    return {"days": len(pending), "resolved": total}


def engine_scorecard(days: int = 30, sb=None) -> list[dict]:
    """
    Per-engine hit rate and net expectancy, net of costs.

    This is the evidence an engine's lifecycle state should rest on. Counts
    setups DETECTED, not trades taken, so an engine that fires often and
    resolves badly is visible even if cost rejection kept you out of them.
    """
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups")
              .select("strategy,outcome,outcome_pct,cost_verdict")
              .gte("trade_date", since).not_.is_("outcome", "null")
              .execute().data or [])

    by: dict[str, dict] = {}
    for r in rows:
        s = by.setdefault(r["strategy"], {"n": 0, "wins": 0, "net": 0.0, "taken": 0})
        s["n"] += 1
        s["net"] += float(r.get("outcome_pct") or 0)
        if r.get("outcome") == "TARGET":
            s["wins"] += 1
        if r.get("cost_verdict") == "TAKEN":
            s["taken"] += 1

    out = []
    for strat, s in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        out.append({
            "strategy": strat, "setups": s["n"], "taken": s["taken"],
            "hit_rate": round(s["wins"] / s["n"] * 100, 1) if s["n"] else 0.0,
            "avg_net_pct": round(s["net"] / s["n"], 3) if s["n"] else 0.0,
        })
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Resolve intraday setup outcomes")
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    ap.add_argument("--scorecard", action="store_true", help="print per-engine stats")
    ap.add_argument("--backfill", action="store_true",
                    help="resolve every past session left unscored")
    ap.add_argument("--days", type=int, default=30, help="backfill window")
    a = ap.parse_args()
    if a.scorecard:
        for row in engine_scorecard():
            logger.info(f"  {row['strategy']:<5} setups {row['setups']:>4} "
                        f"taken {row['taken']:>3}  hit {row['hit_rate']:>5.1f}%  "
                        f"avg net {row['avg_net_pct']:+.3f}%")
    elif a.backfill:
        backfill(a.days)
    else:
        resolve_day(a.date)
