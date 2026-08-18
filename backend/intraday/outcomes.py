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
from config import IST, get_supabase, cfg, cfg_int, today_ist, fetch_all
from intraday import direction as D
# THE SAME CONSTANTS THE DAEMON'S OWN EXIT READS, not copies of them. run.py
# leaves its loop when `is_trading_session()` goes false, which is COOLDOWN_TO;
# the guard below must clear before that instant or the daemon can never score
# its own session again. Two files holding two 15:30s is how a gate and the
# thing it gates drift apart.
from intraday.config import COOLDOWN_TO, MARKET_CLOSE


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


def session_is_over(trade_date: str, now: datetime | None = None) -> tuple[bool, str]:
    """
    True when no further bars can arrive for `trade_date`. F-27, mechanism A.

    WHY A SCORER NEEDS A CLOCK AT ALL.

    `resolve_day` prices a TIMEOUT at `bars[-1]["close"]` — whatever the last
    bar it was handed happens to be — and its work queue is
    `.is_("outcome", "null")`, so a row it has written is never looked at
    again. Both are reasonable alone. Together they are a data-corruption
    engine, because of where the function is called from: `run.py:416`, the
    daemon's `finally` block, which runs on EVERY exit. A crash at 10:12, a
    Ctrl-C at 11:30, a restart to pick up a config change — each one asks Kite
    for the session so far, scores every still-open setup TIMEOUT at a
    mid-morning price, and freezes it there. The evening pipeline's `backfill`
    will not come back for it; it is no longer NULL.

    That is not a rounding error. `intraday_setups` is the table
    `scoring.intraday_priors()` and `hurdle`'s arrival distribution are both
    built from, so one frozen row prices every candidate that arrives after
    it. The measured signature on the live book is 58 same-window
    contradictions, 42 of them a STOP and a TIMEOUT over overlapping windows
    of one symbol — one row scored against the whole session, one against a
    truncated one, and nothing stored saying which was which.

    THE BAR IS THE MARKET CLOSE PLUS A SETTLING BUFFER, AND IT MUST SIT INSIDE
    THE DAEMON'S COOL-DOWN. Kite publishes the closing minute a little after
    15:30, so scoring at 15:30:01 can still price a TIMEOUT at the 15:28
    close — the same defect, one minute wide. But the daemon exits at
    COOLDOWN_TO (15:40) and calls this on the way out, so a buffer that
    reached 15:40 would mean no session is ever scored by the daemon again and
    every day waits for the next evening's pipeline. The key is therefore
    CLAMPED to leave a minute of headroom, loudly, rather than silently
    disabling the path it is meant to protect.

    A PAST date is always over — that is the whole `backfill` population, and
    refusing it would blind the one mechanism that repairs unscored sessions.
    A FUTURE date is never over: a host whose clock or timezone is behind
    would otherwise score a day whose bars do not exist and write TIMEOUT
    across the entire book.

    Returns (ok, why) — `why` is a stable reason string when refusing, and a
    human sentence when allowing.
    """
    now = now or datetime.now(IST)
    try:
        d = date.fromisoformat(str(trade_date))
    except (TypeError, ValueError):
        return False, "unparseable_date"

    today = now.date()
    if d > today:
        return False, "future_session"
    if d < today:
        return True, f"{d} is a past session"

    buf = cfg_int("outcomes_close_buffer_min", 5)
    headroom = int((datetime.combine(date.min, COOLDOWN_TO)
                    - datetime.combine(date.min, MARKET_CLOSE)).total_seconds() // 60)
    if buf >= headroom:
        # Never silently. A key that can push the bar past the daemon's own
        # exit takes the daemon out of the scoring path entirely, and the only
        # symptom would be days quietly arriving at the pipeline unscored.
        logger.warning(
            f"  outcomes_close_buffer_min={buf} would push the scoring bar to or "
            f"past the daemon's {COOLDOWN_TO} exit — clamped to {headroom - 1} "
            f"so the daemon can still score its own session")
        buf = max(headroom - 1, 0)

    bar = IST.localize(datetime.combine(d, MARKET_CLOSE)) + timedelta(minutes=buf)
    if now >= bar:
        return True, f"{now:%H:%M} is past the {bar:%H:%M} bar"
    return False, "session_open"


PROVENANCE_COLS = ("scored_at", "scored_by", "scored_through")


def _provenance_supported(rows: list[dict]) -> bool:
    """
    Does this book carry migration 082's provenance columns? Asked ONCE, free.

    PostgREST fails the WHOLE statement on one unknown column, so sending
    `scored_at` to a book without it would lose the `outcome` write riding
    along with it — a diagnostic column taking down the very data it exists to
    diagnose. Code lands before its migration here as a matter of routine, so
    this cannot be assumed.

    THREE WAYS TO ASK, AND ONLY ONE OF THEM IS FREE OF ITS OWN DEFECT.
    Strip-and-retry (the `_update_stripping_unknown` idiom) costs three failed
    round trips PER ROW, and 14-Aug was 2,289 rows — that is how a diagnostic
    becomes an outage. A one-off `.select("scored_at,...")` probe costs one
    call, but names columns that do not exist in a SELECT list, which is
    exactly what `tools/validate_selects.py` was built to catch: it turned the
    `selects` health check RED, and a health check that is red for a known
    pending migration is how a real warning stops being read.

    The work queue above is already `.select("*")`, so every column of every
    row is in hand. Reading the KEYS of a row costs nothing, cannot raise, and
    cannot go stale. `test_the_work_queue_still_selects_star` pins the
    property this depends on.
    """
    if not rows:
        return False
    missing = [c for c in PROVENANCE_COLS if c not in rows[0]]
    if missing:
        logger.warning(
            f"  outcomes: intraday_setups is missing {', '.join(missing)} — "
            f"apply migration 082. Outcomes are still scored; WHICH RUN scored "
            f"them, and through which bar, is not recorded.")
        return False
    return True


def resolve_day(trade_date: str | None = None, sb=None,
                now: datetime | None = None) -> dict:
    """
    Resolve every unresolved setup for a date. Idempotent.

    Run after the close — from the evening pipeline, or by hand. Setups already
    carrying an outcome are skipped, so re-running is free.

    `now` exists so the session guard is testable without a clock, and so the
    date this scores and the date the guard checks are derived from ONE
    instant. Two clocks would let the guard clear one day while the query
    scored another.

    THE WORK QUEUE IS PAGED, AND THE RESULT SAYS WHETHER IT FINISHED.

    Both halves of that sentence are 15-Aug-2026 repairs to one incident.

    The fetch below used to be a plain `.select("*")`, which PostgREST caps at
    1000 rows with no error. 14-Aug-2026 produced 2289 detections. This function
    ran, was handed 1000 of them, resolved all 1000, and logged
    `1000 resolved — 0 target, 0 stop, 1000 timeout` as a SUCCESS. 1289 rows
    were left with a NULL outcome and nothing anywhere said so. A day that is
    HALF scored is worse than one that is not scored at all, because the health
    check that watches for unscored days looked at the same capped read and
    reported the cap as its count.

    So the return value now carries `remaining` and `complete`. A caller that
    only reads `resolved` cannot tell a finished day from a truncated one, and
    every caller did.
    """
    sb = sb or get_supabase()
    now = now or datetime.now(IST)
    d = trade_date or now.date().isoformat()

    # BEFORE ANY READ. A refusal must not cost a paged fetch of 2,000 rows,
    # and — the point — must not cost a write. `remaining` is None rather than
    # 0 because this run does not know how many are outstanding and must not
    # claim it does; `complete` False is what run.py branches on.
    over, why = session_is_over(d, now=now)
    if not over:
        logger.info(f"  outcomes: {d} is not over ({why}) — scoring refused. "
                    f"A TIMEOUT priced now would be frozen at an intra-session "
                    f"price and never revisited.")
        return {"resolved": 0, "date": d, "remaining": None,
                "complete": False, "reason": why}

    rows = fetch_all(lambda: sb.table("intraday_setups").select("*")
                             .eq("trade_date", d).is_("outcome", "null"))
    if not rows:
        logger.info(f"  outcomes: nothing unresolved for {d}")
        return {"resolved": 0, "date": d, "remaining": 0, "complete": True}

    def _unfinished(reason: str) -> dict:
        """Not scored is NOT the same as nothing to score."""
        return {"resolved": 0, "date": d, "remaining": len(rows),
                "complete": False, "reason": reason}

    try:
        from kite import kite_client
        kite = kite_client.get_kite()
        if not kite:
            logger.warning(f"  outcomes: no broker session — {len(rows)} setup(s) "
                           f"on {d} stay unresolved")
            return _unfinished("no_broker")
    except Exception as e:
        logger.warning(f"  outcomes: broker unavailable — {e}")
        return _unfinished("no_broker")

    interval = cfg("intraday_bar_interval", "minute")
    day = datetime.strptime(d, "%Y-%m-%d").date()
    symbols = sorted({r["symbol"] for r in rows})

    try:
        tokens = kite.ltp([f"NSE:{s}" for s in symbols])
        tok = {k.split(":", 1)[1]: v["instrument_token"] for k, v in (tokens or {}).items()}
    except Exception as e:
        logger.warning(f"  outcomes: token lookup failed — {e}")
        return _unfinished("no_tokens")

    tally = {"TARGET": 0, "STOP": 0, "TIMEOUT": 0, "UNKNOWN": 0}
    resolved = 0
    bar_cache: dict[str, list] = {}

    # WHO SCORED THIS ROW, AND THROUGH WHICH BAR. One probe, one identity, for
    # the whole run. `instance_id()` is host-pid-uuid — the same string the
    # daemon lease writes — because two daemons on one machine, or a daemon
    # and the pipeline, are different RUNS and a hostname cannot separate them.
    prov = _provenance_supported(rows)
    from intraday.lease import instance_id
    run_id, scored_at = instance_id(), now.isoformat()

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
        # `dirn`, NOT `d`. This line read `d = D.normalise(...)` and `d` is the
        # TRADE DATE, bound at the top of the function — so from the first row
        # onward the date was gone, and the success log below (`outcomes {d}:`)
        # printed the last setup's direction: `outcomes LONG: 1000 resolved`.
        # The one line that says WHICH session was scored named a direction
        # instead, which is precisely the line you go looking for when asking
        # why a day was never scored.
        dirn = D.normalise(r.get("direction"))
        outcome, exit_px = "TIMEOUT", float(bars[-1]["close"])
        for b in bars:
            hi, lo = float(b["high"]), float(b["low"])
            if D.is_short(dirn):
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
        pct = D.gain_pct(entry, exit_px, dirn)
        # Net of the round trip, because a gross win under 0.21% is a loss and
        # recording it as a win would teach the wrong lesson.
        cost = float(r.get("cost_pct") or 0)
        payload = {"outcome": outcome, "outcome_pct": round(pct - cost, 3)}
        if prov:
            # `scored_through` is the diagnostic that did not exist. A TIMEOUT
            # is priced at bars[-1]["close"], so THIS is the number that
            # decided it — and a TIMEOUT whose window ends at 11:30 on a
            # session that ran to 15:30 is a frozen row, visible in one query
            # instead of by reasoning about which daemon died when.
            payload.update({
                "scored_at": scored_at,
                "scored_by": run_id,
                "scored_through": bars[-1]["date"].isoformat(),
            })
        try:
            sb.table("intraday_setups").update(payload).eq("id", r["id"]).execute()
            tally[outcome] += 1
            resolved += 1
        except Exception as e:
            logger.debug(f"  outcomes: update failed for {sym}: {e}")

    remaining = len(rows) - resolved
    line = (f"  outcomes {d}: {resolved} of {len(rows)} resolved from "
            f"{len(bar_cache)} symbol fetch(es) — {tally['TARGET']} target, "
            f"{tally['STOP']} stop, {tally['TIMEOUT']} timeout"
            + (f", {tally['UNKNOWN']} unknown" if tally["UNKNOWN"] else ""))
    if remaining:
        # NOT logger.success. A partially scored day previously reported
        # through the same success path as a complete one, so the log gave the
        # operator no way to tell them apart.
        logger.warning(f"{line} — {remaining} STILL UNRESOLVED on {d}")
    else:
        logger.success(line)
    return {"resolved": resolved, "date": d, "remaining": remaining,
            "complete": remaining == 0, **tally}


def unresolved_days(sb=None, days: int = 30) -> list[tuple[str, int]]:
    """
    Past sessions that still carry unscored detections. Newest first.

    Today is excluded: its setups are legitimately unresolved until the close,
    and reporting them as a gap would make the health check cry wolf every
    single trading day — which is how a real warning stops being read.

    THE CAP DID NOT JUST UNDERCOUNT — IT COULD HIDE A WHOLE DATE.

    This read was unpaged, so PostgREST returned the first 1000 unresolved
    rows. On 15-Aug-2026 14-Aug alone had 1289 of them, which means the cap was
    fully consumed by one date and ANY other unscored session would have been
    invisible: not in the health check, and — worse — not in `backfill()`,
    which iterates precisely this list and would therefore never have gone back
    for it. The health check's headline number, "1000 detections were never
    scored", was the row limit wearing a count's clothing.
    """
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()
    today = today_ist().isoformat()
    rows = fetch_all(lambda: sb.table("intraday_setups").select("trade_date")
                             .gte("trade_date", since).is_("outcome", "null"))
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
    stuck: list[str] = []
    for d, n in pending:
        logger.info(f"    backfilling {d} ({n} unresolved)")
        try:
            res = resolve_day(d, sb=sb)
            total += res.get("resolved", 0)
            if not res.get("complete", True):
                stuck.append(f"{d} ({res.get('remaining', '?')} left"
                             + (f", {res['reason']}" if res.get("reason") else "")
                             + ")")
        except Exception as e:
            logger.warning(f"    {d} could not be backfilled — {e}")
            stuck.append(f"{d} ({type(e).__name__})")

    # A BACKFILL THAT COULD NOT FINISH MUST NOT LOOK LIKE ONE THAT DID.
    #
    # This used to sum `resolved` and return. With no broker session — the
    # normal state on a weekend, when the token issued on Friday morning has
    # already passed its 07:30 boundary — every date returned 0 and the caller
    # saw `{"days": 2, "resolved": 0}`, which is exactly what a fully-scored
    # book also produces once `pending` is empty.
    if stuck:
        logger.error(f"  outcomes: backfill could NOT finish {', '.join(stuck)}")
    return {"days": len(pending), "resolved": total,
            "incomplete": stuck, "complete": not stuck}


def alert_unscored(days: int = 30, sb=None) -> bool:
    """
    Push ONE alert when a past session was never scored. Returns True if sent.

    WHY A HEALTH CHECK WAS NOT ENOUGH — 15-Aug-2026.

    `tools.health` has reported this correctly since the day it was written.
    Nobody runs `tools.health` on a Saturday. The 14-Aug gap was found because
    an unrelated session happened to run the sweep and read the line, three
    days after the evidence went missing and one day before the weekly review
    would consume the hole.

    THE ALERT CANNOT LIVE INSIDE THE DAEMON, BECAUSE THE DAEMON NOT RUNNING IS
    THE FAILURE. Nothing SCHEDULES resolution: `resolve_day` is a side-effect
    of `intraday/run.py`'s `finally` block, and only when that daemon held the
    lease. `backfill()` runs from the same block and `unresolved_days` excludes
    today, so a Friday session cannot repair its own remainder — the earliest
    repair is the NEXT trading day's daemon exit. 14-Aug was a Friday and the
    weekly review runs Sunday, so the gap was guaranteed to be consumed before
    anything could close it. An alert fired from that same `finally` block
    would have been silent for exactly the same reason the resolution was.

    So this is a standalone entry point (`--check-and-alert`), meant to be
    scheduled on a clock that does not care whether the market opened, and it
    is also called from the evening pipeline and from the weekly review — the
    consumer whose verdict the missing rows corrupt.

    Routed through `intraday.notifier.Notifier` with `framework="SWING"`, the
    same choice `kite.token_manager.alert_if_stale()` documents: a broken
    learning loop is a whole-account event, and the SWING channel is the one
    that falls back to the configured `alerts.send_alerts` senders.

    Never raises. An alert that cannot be delivered must not abort a pipeline
    step or a review.
    """
    try:
        sb = sb or get_supabase()
        pending = unresolved_days(sb, days)
        if not pending:
            logger.success("  outcomes: every past session is scored")
            return False

        total = sum(n for _, n in pending)
        listing = ", ".join(f"{d} ({n})" for d, n in pending[:6])
        if len(pending) > 6:
            listing += f", +{len(pending) - 6} more"

        logger.error(f"  outcomes: {total} detection(s) across {len(pending)} "
                     f"past session(s) were NEVER SCORED — {listing}")

        from intraday.notifier import Notifier, Action
        action = Action(
            symbol="LEARNING", kind="OUTCOMES_UNSCORED",
            headline=(f"{total} intraday detection(s) across {len(pending)} "
                      f"session(s) were never scored"),
            detail=(f"Unscored: {listing}. The weekly review judges engines on "
                    f"resolved outcomes only, so these sessions are invisible "
                    f"to promotion and retirement decisions and to every "
                    f"prior the allocator ranks on. Needs a live broker "
                    f"session to replay the bars. Fix: "
                    f"python -m intraday.outcomes --backfill"),
            urgency="CRITICAL", framework="SWING",
        )
        notifier = Notifier(sb)
        return bool(notifier.send(action, force=True))
    except Exception as e:
        logger.debug(f"  outcomes: unscored-session alert failed — {e}")
        return False


def engine_scorecard(days: int = 30, sb=None) -> list[dict]:
    """
    Per-engine hit rate and net expectancy, net of costs.

    This is the evidence an engine's lifecycle state should rest on. Counts
    setups DETECTED, not trades taken, so an engine that fires often and
    resolves badly is visible even if cost rejection kept you out of them.
    """
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = fetch_all(lambda: sb.table("intraday_setups")
                             .select("strategy,outcome,outcome_pct,cost_verdict")
                             .gte("trade_date", since)
                             .not_.is_("outcome", "null"))

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
    ap.add_argument("--check-and-alert", action="store_true",
                    help="push a CRITICAL alert if any past session was never "
                         "scored; silent if the book is complete. Writes "
                         "nothing. For a scheduled run on a clock that does "
                         "not depend on the intraday daemon having started.")
    ap.add_argument("--days", type=int, default=30, help="backfill window")
    a = ap.parse_args()
    if a.check_and_alert:
        raise SystemExit(1 if alert_unscored(a.days) else 0)
    if a.scorecard:
        for row in engine_scorecard():
            logger.info(f"  {row['strategy']:<5} setups {row['setups']:>4} "
                        f"taken {row['taken']:>3}  hit {row['hit_rate']:>5.1f}%  "
                        f"avg net {row['avg_net_pct']:+.3f}%")
    elif a.backfill:
        backfill(a.days)
    else:
        resolve_day(a.date)
