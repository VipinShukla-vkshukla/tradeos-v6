"""
Every check this project has, in one run, with one verdict.

    python -m tools.health           full sweep
    python -m tools.health --quick   skip the slow ones (used at launch)

WHY ONE COMMAND
---------------
The checks existed and were never run together. validate_config catches an
incoherent cap, validate_selects catches a column that no longer exists,
simulate catches a stage producing nothing — and each of them was a thing you
had to remember to run. A check you have to remember is a check that runs after
the incident, not before it.

WHAT EACH ONE IS FOR, AND WHY IT IS NOT REDUNDANT
--------------------------------------------------
They fail in genuinely different ways, which is why all of them exist:

  config     numbers that contradict each other, or a setting nothing reads
  storage    the database ceiling arriving on an ordinary Tuesday
  selects    a SELECT naming a column the schema no longer has
  broker     resting orders that do not match the positions they protect
  data       today's inputs actually arrived and are not yesterday's
  simulate   a stage that completes while producing nothing

None of these raise an exception in production. They produce a system that
looks healthy and decides on nothing, which is the failure mode this project
has hit more than any other. Storage is the exception and the reason it FAILS
rather than warns: past the ceiling the system does not decide wrongly, it
stops existing — writes are refused and the evening pipeline produces nothing.

EXIT CODE
---------
0 clean, 1 something is broken. Suitable for a scheduled run.
"""

from __future__ import annotations

import argparse
import io
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


@dataclass
class Check:
    name: str
    what: str                 # what breaking here would mean
    ok: bool = False
    detail: str = ""
    findings: list[str] = field(default_factory=list)
    skipped: bool = False


def _run(fn) -> tuple[bool, str]:
    """Run a check, converting any exception into a failure rather than a crash."""
    try:
        return fn()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"


# ── individual checks ───────────────────────────────────────────────────────
def check_config() -> tuple[bool, str]:
    from tools.validate_config import check_coherence, check_wiring
    from config import get_supabase
    sb = get_supabase()
    bad = [f for f in (check_coherence(sb) + check_wiring(sb))
           if f.severity == "ERROR"]
    if bad:
        return False, "; ".join(f"{f.key} ({f.current})" for f in bad[:4])
    return True, "caps cohere against TOTAL_CAPITAL, every CRITICAL key is read"


def check_selects() -> tuple[bool, str]:
    """
    Does every .select() name columns that exist?

    STRICT IS NOT OPTIONAL HERE, AND THAT IS THE WHOLE FIX.

    validate_selects.main(strict=False) returns `1 if (problems and strict)
    else 0` — it LOGS every broken site and then reports success. This check
    called it with no argument, read only the return code, and announced "every
    SELECT names columns that exist" over the top of its own error output.

    That is the fifth green-while-broken check found in this project, and it is
    the one that mattered most: it is what let allocation/hurdle.py select
    `regime_at_detection`, a column on no migration and written by no code,
    survive all the way into a LIVE veto. PostgREST rejected the whole query,
    the bare except swallowed it, the allocator fell back to its cold-start bar,
    and the intraday book took zero trades while health read clean.

    Demonstrated failing before it was demonstrated passing.
    """
    from tools import validate_selects as vs
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = vs.main(strict=True)
    out = buf.getvalue()
    if rc:
        bad = [l.strip() for l in out.splitlines()
               if "no column(s)" in l or "does not exist" in l or "missing:" in l][:4]
        return False, ("; ".join(bad) or "validate_selects reported failures") + \
            " — PostgREST fails the WHOLE query on one unknown column, so each of "\
            "these returns nothing at all rather than degrading"
    return True, "every SELECT names columns that exist (checked strictly)"


def check_sort_keys() -> tuple[bool, str]:
    """
    Does every `fetch_all` sort on a column that actually exists?

    THIS IS check_selects' BLIND SPOT, AND IT COST THE SWING OUTCOME SCORER.

    `fetch_all(order_by="id")` is the default, and `id` is not universal in this
    schema: `stock_data_daily`, `signal_output_daily`, `open_positions` and the
    views have no such column. PostgREST answers a sort on a missing column with
    42703 and fails the WHOLE query, so the reader returns nothing at all.

    `check_selects` cannot see this. It validates the columns named in
    `.select()`; the sort key is an ARGUMENT to a Python function, never appears
    in a select string, and is supplied by a default the call site does not
    write down.

    Found 15-Aug-2026: the previous session converted the two 91%-truncating
    price reads — its own headline fix, "the worst thing found in this stage" —
    to `fetch_all` without a sort key. Both read `stock_data_daily`. Calling
    `performance_tracker._load_outcomes_for_date_range` raised 42703 on the
    first page, so the swing brain's forward-return scorer went from silently
    reading 9% of the prices to reading none and raising. Two of 45 call sites
    were wrong and 43 were fine, which is exactly the ratio that survives
    review by eye.

    A live schema probe, not a grep: "the code names this column" and "this
    column exists" are different claims, and only one of them can be checked
    offline.
    """
    import re as _re
    from pathlib import Path as _Path
    from config import get_supabase
    sb = get_supabase()
    root = _Path(__file__).resolve().parent.parent

    seen: dict[str, set] = {}

    def _cols(table: str) -> set:
        if table not in seen:
            try:
                r = sb.table(table).select("*").limit(1).execute().data
                seen[table] = set(r[0].keys()) if r else set()
            except Exception:
                seen[table] = set()
        return seen[table]

    bad, checked = [], 0
    for path in sorted(root.rglob("*.py")):
        if any(x in path.parts for x in ("__pycache__", "db")) or " - Copy" in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _re.finditer(r'fetch_all\(', text):
            seg = text[m.end():m.end() + 700]
            tm = _re.search(r'\.table\(\s*["\'](\w+)["\']\s*\)', seg)
            if not tm:
                continue
            table = tm.group(1)
            cols = _cols(table)
            if not cols:                      # empty or unreadable — cannot judge
                continue
            checked += 1
            ob = _re.search(r'order_by\s*=\s*["\']([^"\']+)["\']', seg)
            key = ob.group(1) if ob else "id"
            missing = [k.strip() for k in key.split(",") if k.strip() not in cols]
            if missing:
                line = text[:m.start()].count("\n") + 1
                bad.append(f"{path.relative_to(root).as_posix()}:{line} sorts "
                           f"{table} on {missing} which it does not have")

    # A SCANNER THAT MATCHES NOTHING PASSES FOREVER.
    if checked < 20:
        return False, (f"only {checked} fetch_all call sites were resolved against the "
                       f"schema — the scanner has stopped recognising them, and a check "
                       f"that cannot fail is not a check")
    if bad:
        return False, ("; ".join(bad[:3]) +
                       f" ({len(bad)} total) — PostgREST fails the WHOLE query on an "
                       f"unknown sort column, so these readers return nothing at all")
    return True, f"all {checked} fetch_all reads sort on a column that exists"


def check_broker_consistency() -> tuple[bool, str]:
    """
    Do the resting broker orders match the positions they are supposed to protect?

    Three distinct ways this goes wrong, all silent:
      · a GTT for a symbol no longer held — a live sell against nothing
      · a GTT for a PAPER position — a real order for simulated stock
      · a live position with no GTT — unprotected if the daemon is down
    """
    from config import get_supabase
    from execution.gates import gtt_enabled
    if not gtt_enabled():
        return True, "GTT disabled — nothing resting to reconcile"

    sb = get_supabase()
    rows = (sb.table("open_positions").select("symbol,mode,status,current_qty,actual_qty")
              .eq("status", "ACTIVE").execute().data or [])
    live = {r["symbol"] for r in rows if (r.get("mode") or "LIVE").upper() != "PAPER"}
    paper = {r["symbol"] for r in rows if (r.get("mode") or "LIVE").upper() == "PAPER"}

    # list_gtts() returns None — never {} — when it cannot confirm what is
    # resting (no session, or the call failed). Reporting every live position
    # as unprotected when the truth is "cannot see" sends you hunting for a
    # problem that may not be there, and buries the real finding, which is
    # that the session or the connection died.
    try:
        from kite import kite_client
        if not kite_client.get_kite():
            return True, ("cannot verify — no broker session. Resting GTTs are "
                          "unaffected by this; fix the session and re-check.")
    except Exception:
        return True, "cannot verify — broker unavailable"

    try:
        from execution.gtt_manager import list_gtts
        existing = list_gtts()
    except Exception as e:
        return True, f"could not read GTTs ({str(e)[:60]}) — broker session needed"

    if existing is None:
        return True, ("cannot verify — GTT list call failed. Resting GTTs are "
                      "unaffected by this; fix the session/connectivity and re-check.")

    resting = set(existing)
    problems = []
    for s in sorted(resting & paper):
        problems.append(f"{s}: REAL GTT for a PAPER position")
    for s in sorted(resting - live - paper):
        problems.append(f"{s}: GTT resting but no position")
    for s in sorted(live - resting):
        problems.append(f"{s}: live position with NO broker stop")
    # set(existing) only sees SYMBOLS, never COUNT — a duplicate GTT on a
    # symbol that is otherwise correctly protected is invisible to every
    # comparison above this line. This is exactly what let 6 positions carry
    # 12 live SELL GTTs on 2026-08-06 while this check reported a clean match
    # ("6 GTT(s) match 6 live position(s)") — it was counting symbols, not GTTs.
    for s, gs in sorted(existing.items()):
        if len(gs) > 1:
            problems.append(f"{s}: {len(gs)} duplicate GTTs resting (ids "
                             f"{','.join(str(g.gtt_id) for g in gs)})")
    if problems:
        shown = "; ".join(problems[:4])
        if len(problems) > 4:
            shown += f"; +{len(problems) - 4} more"
        return False, shown
    total = sum(len(gs) for gs in existing.values())
    return True, f"{total} GTT(s) match {len(live)} live position(s)"


def check_data_freshness() -> tuple[bool, str]:
    """
    Did today's inputs actually arrive?

    A pipeline that runs on yesterday's data completes successfully and decides
    on a market that has moved. Staleness is invisible unless it is checked.
    """
    from config import get_supabase, today_ist
    sb = get_supabase()
    today = str(today_ist())
    stale = []
    for table, col, tol_days in (("signal_output_daily", "date", 4),
                                 ("stock_data_daily", "date", 4)):
        try:
            r = (sb.table(table).select(col).order(col, desc=True)
                   .limit(1).execute().data or [])
            if not r:
                stale.append(f"{table}: EMPTY")
                continue
            latest = str(r[0][col])[:10]
            from datetime import date
            gap = (date.fromisoformat(today) - date.fromisoformat(latest)).days
            if gap > tol_days:
                stale.append(f"{table}: {gap}d old ({latest})")
        except Exception as e:
            stale.append(f"{table}: {str(e)[:50]}")
    if stale:
        return False, "; ".join(stale)
    return True, "signal and price data are current"


def check_kite() -> tuple[bool, str]:
    """Probe the session rather than trusting a validity flag."""
    from config import get_supabase
    sb = get_supabase()
    try:
        from kite import kite_client
        prof = kite_client.get_kite().profile()
    except Exception as e:
        return False, f"Kite call failed: {str(e)[:110]}"

    # Zerodha's allowlist is IPv4-only. On a dual-stack network api.kite.trade
    # resolves v6-first, so orders leave from an address that cannot be
    # allowlisted — while this very check, asking a v4 endpoint, reports a
    # perfect match. That combination cost a live session: every exit rejected
    # with "IP (2402:e280:...) is not allowed", and every readiness check green.
    #
    # kite_client forces v4 at import. This verifies the force actually took,
    # because a check that cannot fail is not a check.
    # Host-scoped like everything else below it: DNS resolution is a property of
    # the machine asking, so the message names it rather than implying the
    # system as a whole resolves this way.
    import socket as _sock
    host = _sock.gethostname()
    try:
        fams = {f for f, *_ in _sock.getaddrinfo("api.kite.trade", 443)}
        if _sock.AF_INET6 in fams:
            return False, (f"on '{host}', api.kite.trade still resolves over IPv6 "
                           f"— orders from this machine would leave from a v6 "
                           f"address that Zerodha's IPv4-only allowlist can never "
                           f"match")
    except Exception:
        pass

    # An allowlist mismatch does not surface until an order is rejected, which
    # is mid-session, which is the worst time to discover it.
    #
    # BUT IT IS ONLY A MISMATCH ON THE MACHINE THAT PLACES ORDERS.
    #
    # This check ran on whatever host invoked it and reported its findings as
    # though they described the system. They did not. Orders leave from ONE
    # machine — the Oracle VCN, whose static public IP is correctly allowlisted.
    # The laptop has a dynamic ISP address that changes on every reconnect and
    # is allowlisted nowhere, deliberately. So run from the laptop this printed
    #
    #     public IP is 103.197.74.232 but only 103.197.75.33 is recorded as
    #     allowlisted — order placement will be REJECTED from this machine
    #
    # which is literally true of the laptop and says nothing whatsoever about
    # whether tomorrow's exits will fill. It was recorded as F-12, "the IP
    # allowlist is stale RIGHT NOW, live, before tomorrow's open", and the four
    # "distinct addresses" behind it across four sessions were four DHCP leases
    # on a machine that has never sent an order.
    #
    # A wrong alarm is not a lesser fault than a missing one; it is the fault
    # that teaches an operator to ignore this output. So the comparison is only
    # EVALUATED where it means something, it always names the host it is talking
    # about, and where it does not apply it says which machine it would apply to
    # instead of going quiet.
    ip = ""
    ip_note = ""
    try:
        from execution.order_manager import host_permits_live
        cfgrows = (sb.table("system_config").select("key,value")
                     .in_("key", ["kite_allowlisted_ip", "live_order_host",
                                  "intraday_lease_primary_host"])
                     .execute().data or [])
        conf = {r["key"]: (r.get("value") or "") for r in cfgrows}
        # The SAME function preflight gates live orders on, not a second copy of
        # the rule — a health check that disagrees with the gate it reports on is
        # how you get a green board over a refused order.
        order_host = (conf.get("live_order_host", "").strip()
                      or conf.get("intraday_lease_primary_host", "").strip())
        this_host_places_orders, _ = host_permits_live(host, order_host)

        import urllib.request
        ip = urllib.request.urlopen("https://api.ipify.org",
                                    timeout=8).read().decode().strip()
        # TWO MACHINES, TWO IPs, ONE KEY.
        #
        # Zerodha's console accepts SEVERAL allowlisted IPs, so the key is a
        # comma-separated list and this asks for membership, not equality.
        allowed = [p.strip() for p in conf.get("kite_allowlisted_ip", "").split(",")
                   if p.strip()]

        if order_host and not this_host_places_orders:
            # Not the order-placing machine: this host's address cannot affect
            # whether orders fill, so it is not compared. Note it and carry on —
            # the broker's own verdict below is read from a table both machines
            # share and IS meaningful from here, so returning early would trade
            # one wrong answer for a missing one.
            ip_note = (f", read-only from '{host}' ({ip or 'IP unknown'}) — this "
                       f"machine does not place orders, so its address is not "
                       f"compared against the allowlist; live orders go out from "
                       f"'{order_host}'")
        elif allowed and ip not in allowed:
            return False, (f"'{host}' — the machine that places live orders — has "
                           f"public IP {ip}, but only {', '.join(allowed)} "
                           f"{'is' if len(allowed) == 1 else 'are'} recorded as "
                           f"allowlisted. Order placement will be REJECTED from "
                           f"this address")
        elif ip and allowed:
            ip_note = (f", '{host}' places live orders and its IPv4 {ip} is in "
                       f"recorded kite_allowlisted_ip")
        elif ip:
            ip_note = (f", IPv4 {ip} on '{host}' (no kite_allowlisted_ip recorded "
                       f"to compare against)")
    except Exception:
        pass

    # THE BROKER'S OWN VERDICT, which is the only ground truth here.
    #
    # Everything above compares this machine's IP against a value the operator
    # typed into system_config. That is a note-to-self, not the Zerodha
    # developer console, and the two can disagree indefinitely with nothing to
    # notice. On 2026-08-06 they did: kite_allowlisted_ip said 103.197.74.141,
    # the live IP WAS 103.197.74.141, this check printed "IPv4 103.197.74.141
    # matches allowlist" — and Zerodha rejected a live KIMS exit from that exact
    # address, because the console did not actually hold it. A check comparing
    # local bookkeeping against itself cannot fail.
    #
    # An order rejection is already recorded, by order_manager, in a table this
    # check can read. So ask the broker's answer rather than our own.
    try:
        from config import today_ist
        bad = (sb.table("intraday_broker_log")
                 .select("ts,symbol,detail")
                 .eq("action", "BLOCKED_PERMANENT")
                 .gte("ts", str(today_ist()))
                 .order("ts", desc=True).limit(1).execute().data or [])
        if bad:
            r = bad[0]
            # ts is a timestamptz and PostgREST renders it in UTC. Printing that
            # raw would show a rejection at "04:44" for something the operator
            # watched happen at 10:14 — so convert to the clock they were
            # actually looking at.
            from datetime import datetime as _dt
            from config import IST as _IST
            try:
                when = _dt.fromisoformat(
                    str(r.get("ts")).replace("Z", "+00:00")).astimezone(_IST).strftime("%H:%M IST")
            except Exception:
                when = str(r.get("ts"))[:16]
            return False, (
                f"the BROKER rejected an order today ({r.get('symbol')} at "
                f"{when}) with a configuration error, "
                f"whatever this machine's IP says: "
                f"\"{(r.get('detail') or '')[:90]}\" — orders are DEAD until "
                f"this is fixed at the broker AND the daemon is restarted")
    except Exception as e:
        return False, (f"could not read the broker rejection log ({str(e)[:50]}) "
                       f"— cannot confirm orders are accepted")

    return True, (f"session live for {prof.get('user_id')}"
                  + (ip_note or f", IPv4 forced on '{host}'")
                  + ", no broker config rejection today")


def check_capital() -> tuple[bool, str]:
    """
    Does TOTAL_CAPITAL still match what the broker account actually holds?

    capital_status() was only ever called from position_lifecycle, on its own
    30-min GitHub Actions cadence — never from here, so a BLOCK-level gap
    could sit on the dashboard while `health` reported nothing. Found
    2026-08-21: configured Rs 30,000 vs Rs 20,307 actual, 32% short, invisible
    to this command. Runs live and persists, refreshing the dashboard's
    snapshot too.

    UNKNOWN (no broker session when this ran) passes — "could not check" must
    not read the same as a measured BLOCK.
    """
    from config import get_supabase
    from control.capital_check import capital_status
    sb = get_supabase()
    st = capital_status(sb, persist=True)
    if st["severity"] == "BLOCK":
        return False, st["message"]
    if st["severity"] == "UNKNOWN":
        return True, f"not verified this run — {st['message']}"
    return True, st["message"]


def check_daemon() -> tuple[bool, str]:
    """
    Is a monitor alive, and WHERE?

    The lease answers both, because it is the same row the daemons use to decide
    which of them may act. A stale lease during market hours means nothing is
    watching your positions — and that is silent: the daemon does not announce
    its own death, and the dashboard keeps showing the last prices it wrote.

    Outside market hours a stale lease is correct, not a fault.
    """
    from datetime import datetime, timezone
    from config import get_supabase
    from intraday.config import is_trading_session, is_holiday

    sb = get_supabase()
    rows = (sb.table("intraday_daemon_lease")
              .select("holder,hostname,expires_at,acquired_at")
              .eq("id", 1).execute().data or [])
    in_session = is_trading_session() and not is_holiday()

    if not rows:
        return (not in_session,
                "no monitor has ever run" if not in_session
                else "NO MONITOR RUNNING — positions are unwatched")

    r = rows[0]
    host = r.get("hostname") or "?"
    try:
        exp = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - exp).total_seconds()
    except Exception:
        return False, f"lease timestamp unreadable: {r.get('expires_at')}"

    # DO THE TWO MACHINES AGREE ON THE SIZE OF THE ACCOUNT?
    #
    # TOTAL_CAPITAL is read from a per-machine .env, but it describes the one
    # shared account. On 2026-08-06 the laptop had ₹30,000 and the server
    # ₹20,000, both set explicitly (neither log carried the fallback warning),
    # so every position size depended on which daemon held the lease — a 50%
    # sizing difference on identical signals, with nothing comparing the two.
    # capital_check records the author of its snapshot for exactly this.
    split = ""
    try:
        import json as _json, socket as _sock
        from config import TOTAL_CAPITAL as _mine
        snap = (sb.table("system_config").select("value")
                  .eq("key", "capital_snapshot").execute().data or [])
        if snap:
            s = _json.loads(snap[0]["value"] or "{}")
            theirs, who = s.get("configured"), s.get("hostname")
            if (theirs and who and who != _sock.gethostname()
                    and abs(float(theirs) - float(_mine)) > 1.0):
                split = (f" — ⚠ SPLIT BRAIN: '{who}' sizes against "
                         f"₹{float(theirs):,.0f} but this machine uses "
                         f"₹{float(_mine):,.0f}. One account, one number: whichever "
                         f"daemon holds the lease decides every position size. "
                         f"Fix TOTAL_CAPITAL in the other machine's .env")
    except Exception:
        pass

    if age < 0:
        if split:
            return False, f"monitor ALIVE on '{host}'{split}"
        return True, f"monitor ALIVE on '{host}' (lease valid {abs(age):.0f}s more)"
    if not in_session:
        return True, f"no monitor — outside market hours (last ran on '{host}'){split}"
    return False, (f"monitor on '{host}' STOPPED renewing {age / 60:.0f} min ago "
                   f"during market hours — positions are unwatched{split}")


def check_open_exits() -> tuple[bool, str]:
    """
    Is a SELL that this system decided on still sitting unfilled?

    17-Aug-2026, GABRIEL. EXIT_STALL fired at 09:15:13 and placed a SELL LIMIT
    at 1460.60. gtt_manager cancelled the protective GTT 48 seconds later,
    because the position had gone to status CLOSING and no longer looked held.
    The order then rested above a falling market, unfilled, until 11:48 — when
    the OPERATOR repriced it by hand. For two and a half hours a live CNC
    position held real stock with no stop and an unfillable limit, and nothing
    anywhere reported it.

    `pending` is the mirror of this check and does not cover it: it watches
    unfilled ENTRIES, where the failure mode is a phantom position. Here the
    failure mode is the opposite and worse — a real position, already decided
    against, still exposed and no longer protected.

    FAILS rather than warns. An exit the system has committed to and not
    completed is the one state in this book where doing nothing keeps costing
    money, and it is invisible on the dashboard, which reads open_positions
    rather than the order book.
    """
    from config import cfg_float
    from datetime import datetime
    from config import IST
    try:
        from kite import kite_client
        kite = kite_client.get_kite()
        if not kite:
            return True, "no broker session — cannot check open exits"
        orders = kite.orders() or []
    except Exception as e:
        return False, f"could not read the order book to check for stranded exits: {e}"

    from execution.exit_orders import stale_exits
    limit_s = cfg_float("exit_order_stale_alert_s", 300.0)
    stale = stale_exits(orders, datetime.now(IST), limit_s)
    if not stale:
        working = sum(1 for o in orders
                      if (o.get("transaction_type") or "").upper() == "SELL")
        return True, (f"no SELL order has been open longer than {limit_s / 60:.0f} min "
                      f"({working} sell order(s) seen today)")

    worst = max(stale, key=lambda o: o["_age_s"])
    names = ", ".join(sorted({str(o.get("tradingsymbol")) for o in stale})[:4])
    return False, (
        f"{len(stale)} SELL order(s) unfilled past {limit_s / 60:.0f} min — {names}"
        f" (worst {worst.get('tradingsymbol')} @ ₹{worst.get('price')}, "
        f"{worst['_age_s'] / 60:.0f} min old). The position is still held, its GTT "
        f"may already be cancelled, and the decision to exit has not completed. "
        f"Reprice it or send it MARKET; enable exit_order_reprice_enabled to have "
        f"the daemon do this itself")


def check_pending_fills() -> tuple[bool, str]:
    """
    Is any entry stuck awaiting fill confirmation past when it should have
    resolved?

    05-Aug-2026: TMCV was submitted as a LIMIT day order, recorded as a live
    position immediately, and never actually filled — nothing checked, so it
    sat as a genuine-looking holding for two sessions before reconcile
    invented a sale to explain its absence from Kite. The fix
    (engine._resolve_pending_fills, on the slow timer) writes status=
    'PENDING_FILL' at submission and only promotes to ACTIVE once Kite
    confirms COMPLETE.

    A row still PENDING_FILL from a PAST session cannot legitimately still be
    OPEN at the broker — a day order resolves one way or another by the close
    of the session it was placed in. Surviving past that boundary means
    _resolve_pending_fills could not resolve it (no order_id, a broker call
    that keeps failing, or Kite's order history has already rolled off) —
    which needs a human, not another retry.

    NOT a fault for a row still pending from TODAY'S session: an unfilled
    LIMIT order resting at its price is the normal state of an entry that has
    not triggered yet, for as long as the market is open.
    """
    from config import get_supabase, today_ist

    sb = get_supabase()
    rows = (sb.table("open_positions").select("symbol,entry_date,entry_order_id,synced_at")
              .eq("status", "PENDING_FILL").execute().data or [])
    if not rows:
        return True, "no entries awaiting fill confirmation"

    today = today_ist().isoformat()
    stuck = [r for r in rows
             if str(r.get("entry_date") or "")[:10] < today or not r.get("entry_order_id")]
    if stuck:
        names = ", ".join(f"{r['symbol']} (since {r.get('entry_date')})" for r in stuck[:5])
        return False, (f"{len(stuck)} entry/entries stuck PENDING_FILL past their own "
                       f"session, or missing an order_id to resolve against: {names}. "
                       f"_resolve_pending_fills could not confirm these — check manually "
                       f"against Kite and clear the row once you know what actually "
                       f"happened.")

    today_pending = [r["symbol"] for r in rows]
    return True, (f"{len(rows)} entry/entries awaiting fill confirmation from today's "
                  f"session, none overdue: {', '.join(today_pending)}")


def check_pending_fill_duplicates() -> tuple[bool, str]:
    """
    Did a SWING symbol get bought twice within minutes — the F-67
    HINDCOPPER shape?

    24-Aug-2026: `_maybe_enter_swing` set `self._pending_fills[sym]` then
    immediately called `load_state()`, which rebuilds that dict from a
    fresh DB read that had not yet caught up with the row this exact call
    just wrote — silently erasing the guard. 15 retries were blocked by
    `order_manager`'s own 5-minute duplicate-order cooldown before that
    window lapsed and a second real BUY landed for the same name. Fixed
    (`intraday/engine.py`, the two lines reordered), but that is one fix
    for one incident, not a guarantee the shape never recurs — this is the
    standing check for it, distinct from `check_pending_fills` above,
    which asks a different question (is a row stuck unresolved) than this
    one (did an order actually double up).

    Two real ORDER PLACED events for the same (symbol, BUY) with no SELL
    between them, inside a short window, can only mean the decision-layer
    guard — not just the order-layer cooldown, which exists precisely to
    catch what the guard misses — failed to recognise the symbol as
    already being acted on. A SELL between two BUYs resets the window
    deliberately: closing a position and re-entering it later the same
    day on a fresh signal (HINDCOPPER's own 24-Aug re-entry, itself
    legitimate) must not be flagged as a duplicate of the trade that
    already closed.
    """
    from datetime import datetime, timedelta
    from collections import defaultdict
    from config import get_supabase, today_ist

    sb = get_supabase()
    since = (today_ist() - timedelta(days=7)).isoformat()
    rows = (sb.table("intraday_broker_log")
              .select("symbol,side,ts")
              .eq("channel", "ORDER").eq("action", "PLACED")
              .eq("framework", "SWING")
              .gte("ts", since).order("ts").execute().data or [])

    WINDOW_MIN = 10   # order_manager's own duplicate cooldown is 5 minutes;
                      # double that catches a near-miss too, not just an
                      # exact repeat of the F-67 timing.
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)

    def _parse(ts):
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

    doubles = []
    for sym, events in by_symbol.items():
        events.sort(key=lambda r: r["ts"])
        last_buy = None
        for r in events:
            side = (r.get("side") or "").upper()
            if side == "SELL":
                last_buy = None   # resets the window — a fresh BUY after
                                  # this is a new decision, not a repeat
                continue
            if side != "BUY":
                continue
            if last_buy is not None:
                gap = (_parse(r["ts"]) - _parse(last_buy["ts"])).total_seconds()
                if gap <= WINDOW_MIN * 60:
                    doubles.append((sym, last_buy["ts"], r["ts"], gap))
            last_buy = r

    if doubles:
        names = ", ".join(f"{s} ({a} -> {b}, {g:.0f}s apart)"
                          for s, a, b, g in doubles[:5])
        return False, (f"{len(doubles)} SWING symbol(s) bought twice within "
                       f"{WINDOW_MIN} minutes, no SELL in between, in the last "
                       f"7 days: {names}. Same shape as F-67 (HINDCOPPER, "
                       f"24-Aug) — check the pending-fill guard "
                       f"(intraday/engine.py::_maybe_enter_swing) and "
                       f"reconcile the resulting position quantity against "
                       f"the broker.")
    return True, f"no duplicate SWING buys in the last 7 days ({len(rows)} orders placed)"


def check_pending_scale_ins() -> tuple[bool, str]:
    """
    Is a Stage E7 scale-in add stuck awaiting fill confirmation past when
    it should have resolved?

    Same question `check_pending_fills` asks about a fresh entry, asked
    about the ADD instead — the same TMCV-shaped risk applies: a LIMIT
    day order that never actually filled must not be silently forgotten.
    `scale_in_status` is a column distinct from the row's own `status`
    (migration 114) specifically so the original tranche keeps being
    read/managed everywhere while only the add is unresolved — which
    means nothing else in this check registry would ever notice a stuck
    add on its own; it needs its own standing check exactly as the entry
    side got one after TMCV.
    """
    from config import get_supabase, today_ist

    sb = get_supabase()
    rows = (sb.table("open_positions")
              .select("symbol,synced_at,scale_in_order_id,scaled_in_at")
              .eq("scale_in_status", "PENDING_FILL").execute().data or [])
    if not rows:
        return True, "no scale-in adds awaiting fill confirmation"

    today = today_ist().isoformat()
    stuck = [r for r in rows
             if str(r.get("synced_at") or "")[:10] < today or not r.get("scale_in_order_id")]
    if stuck:
        names = ", ".join(f"{r['symbol']} (since {r.get('synced_at')})" for r in stuck[:5])
        return False, (f"{len(stuck)} scale-in add(s) stuck PENDING_FILL past their "
                       f"own session, or missing an order_id to resolve against: "
                       f"{names}. _resolve_pending_scale_ins could not confirm "
                       f"these — check manually against Kite; the ORIGINAL "
                       f"position is unaffected and still fully managed either way.")

    return True, (f"{len(rows)} scale-in add(s) awaiting fill confirmation from "
                  f"today's session, none overdue: "
                  f"{', '.join(r['symbol'] for r in rows)}")


def check_sector_concentration_risk() -> tuple[bool, str]:
    """
    Does a meaningful fraction of the live SWING book sit in sectors that
    are ROTATING AWAY from it right now?

    Track E, Stage E4, the book-wide half of the participation/sector-decay
    work — `evaluate_exit()`'s own sector-decay multiplier (F-71) reads
    `sector_strength` PER POSITION, against that position's own sector. It
    has no view of the BOOK: three positions each individually tolerable at
    x0.75 tightening can still mean the whole book is leaning into one
    fading trade at once, which no per-position check can see by
    construction. This is a standing, read-only diagnostic — it changes
    nothing, gates nothing, only tells the operator what four or five lines
    of manual SQL would otherwise have to.

    Confirmed live, 24-Aug-2026: 2 of 3 open SWING positions (HINDCOPPER —
    metals & mining, AARTIIND — chemicals) sit in sectors reading WEAKENING
    today; both already carry the per-position sector-decay shadow line.
    """
    from config import get_supabase

    sb = get_supabase()
    pos_rows = (sb.table("open_positions")
                  .select("symbol,sector")
                  .eq("framework", "SWING").eq("status", "ACTIVE")
                  .execute().data or [])
    if not pos_rows:
        return True, "no open SWING positions to check"

    dr = (sb.table("sector_strength").select("date")
            .order("date", desc=True).limit(1).execute().data or [])
    latest_date = dr[0]["date"] if dr else None
    srows = ((sb.table("sector_strength")
                .select("sector,sector_state,rank_delta_5d")
                .eq("date", latest_date).execute().data or [])
             if latest_date else [])
    state_by_sector = {r["sector"]: r for r in srows if r.get("sector")}

    weakening = []
    for p in pos_rows:
        sym, sector = p.get("symbol"), str(p.get("sector") or "").strip()
        st = state_by_sector.get(sector)
        if st and st.get("sector_state") == "WEAKENING":
            weakening.append((sym, sector, st.get("rank_delta_5d")))

    frac = len(weakening) / len(pos_rows)
    THRESHOLD = 0.5   # a bare majority of the book, not a single position —
                      # per-position tightening already handles one name.
    if frac >= THRESHOLD:
        names = ", ".join(f"{s} ({sec}, rank_delta_5d={d})"
                          for s, sec, d in weakening)
        return False, (
            f"{len(weakening)}/{len(pos_rows)} open SWING positions "
            f"({frac:.0%}) sit in sectors reading WEAKENING today: {names}. "
            f"Each is already tightened individually by swing_sector_decay_"
            f"enabled if armed (and exempted there when the position's own "
            f"volume is holding up — sector-level weakness alone does not "
            f"veto a demonstrated individual leader) — this flags the "
            f"BOOK-wide concentration a per-position check cannot see. Not "
            f"an order to act, and not a case for avoiding the sector "
            f"outright: a genuinely strong individual name (rising volume, "
            f"holding relative strength) can still lead through a weak "
            f"group. A diagnostic for the operator's own judgement.")
    return True, (f"{len(weakening)}/{len(pos_rows)} open SWING positions "
                  f"in WEAKENING sectors, below the {THRESHOLD:.0%} "
                  f"concentration threshold")


def _same_day_drift_symbols(reconcile_rows: list[dict],
                            closed_rows: list[dict]) -> list[dict]:
    """
    PURE. Which closed positions entered on the SAME calendar day (IST) as
    a QTY_REDUCED/QTY_INCREASED reconcile event for that symbol — the
    exact signature the F-84/F-85 HINDCOPPER incident had (25-Aug-2026).

    WHY THIS SIGNATURE, SPECIFICALLY: a same-day quantity correction on the
    ENTRY date means the fills that established this position's qty/
    entry_price landed the same session reconcile also had to patch drift
    for it — the precise condition under which `_merge_day_position()`'s
    own fix (F-84) matters, AND, separately, the condition under which an
    entry-side upsert-over-first bug (F-67's shape) can have overwritten
    `entry_price` with only the LAST fill rather than a true blend. F-84's
    own code fix closes the qty half of this permanently; it cannot close
    the entry_price half, because a real broker-side moving-average cost
    basis (F-85) is not a bug this codebase can safely auto-correct
    against — some of these flags will be legitimate broker accounting,
    not another error. This is a DETECTION aid, matching this project's
    "propose, never auto-apply" rule: it names which rows are worth a
    manual check against Kite's own Holdings page, never corrects one.

    Pure over already-fetched rows so it is testable without Supabase I/O
    — the caller does the fetching and the IST day-boundary conversion is
    the only "real" logic here, done with a fixed tzinfo, not the clock.
    """
    from datetime import datetime
    from config import IST

    def _ist_date(ts) -> str | None:
        try:
            return (datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                     .astimezone(IST).date().isoformat())
        except Exception:
            return None

    drift_days: dict[tuple[str, str], list[str]] = {}
    for r in reconcile_rows:
        if r.get("action") not in ("QTY_REDUCED", "QTY_INCREASED"):
            continue
        d = _ist_date(r.get("run_at"))
        sym = r.get("symbol")
        if not (d and sym):
            continue
        drift_days.setdefault((sym, d), []).append(r.get("action"))

    flagged = []
    for pos in closed_rows:
        sym = pos.get("symbol")
        entry_date = str(pos.get("entry_date") or "")[:10]
        key = (sym, entry_date)
        if key in drift_days:
            flagged.append({
                "id": pos.get("id"), "symbol": sym, "entry_date": entry_date,
                "exit_date": pos.get("exit_date"),
                "reconcile_actions": drift_days[key],
            })
    return flagged


def check_same_day_reconcile_drift() -> tuple[bool, str]:
    """
    Did a recently closed position's ENTRY DATE coincide with a same-day
    quantity reconcile drift for that symbol — the exact HINDCOPPER
    shape (F-84/F-85, 25-Aug-2026)?

    That incident was found only because the operator happened to compare
    the dashboard against a broker export by hand, weeks after — nothing
    in this project's own standing checks would have surfaced it. This
    closes that gap going forward: not a claim that a flagged row IS
    wrong (`_merge_day_position()`'s own fix, F-84, already prevents the
    QUANTITY half of this bug from recurring; a flag here may be nothing
    more than an ordinary broker-side cost-basis update, F-85's own
    finding), but a claim that it is WORTH CHECKING against Kite's own
    Holdings page before trusting the row's entry_price/P&L — the same
    verification this project's own tooling cannot do itself, since
    Kite's live trade history is current-day only and this project keeps
    no earlier record of a symbol's cost basis than its own entry write.

    14-day window: long enough to catch a row before it is forgotten,
    short enough that this stays cheap on every run.
    """
    from datetime import timedelta
    from config import get_supabase, today_ist

    sb = get_supabase()
    since = (today_ist() - timedelta(days=14)).isoformat()

    closed_rows = (sb.table("closed_positions")
                     .select("id,symbol,entry_date,exit_date,product")
                     .eq("product", "CNC")
                     .gte("exit_date", since).execute().data or [])
    if not closed_rows:
        return True, "no CNC positions closed in the last 14 days"

    reconcile_rows = (sb.table("position_reconcile_log")
                         .select("symbol,action,run_at")
                         .in_("action", ["QTY_REDUCED", "QTY_INCREASED"])
                         .gte("run_at", since).execute().data or [])

    flagged = _same_day_drift_symbols(reconcile_rows, closed_rows)
    if flagged:
        names = ", ".join(f"{f['symbol']} (id={f['id']}, entered {f['entry_date']}, "
                          f"{'/'.join(f['reconcile_actions'])})" for f in flagged[:5])
        return False, (
            f"{len(flagged)} closed CNC position(s) in the last 14 days entered "
            f"on the same day as a QTY_REDUCED/QTY_INCREASED reconcile event for "
            f"that symbol: {names}. Not necessarily wrong — verify entry_price "
            f"and actual_qty against Kite's own Holdings page before trusting "
            f"the row; this is the exact shape F-84/F-85 found HINDCOPPER "
            f"corrupted by. See docs/FINDINGS.md F-84/F-85/F-86.")
    return True, (f"{len(closed_rows)} CNC position(s) closed in the last 14 days, "
                  f"none share an entry-day reconcile drift event")


def check_simulate() -> tuple[bool, str]:
    """The slow one: run both frameworks end to end and confirm stages produce output."""
    from config import get_supabase
    from tools.simulate import simulate_swing, simulate_intraday
    sb = get_supabase()
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        s = simulate_swing(sb)
        i = simulate_intraday(sb)
    notes = []
    if s.get("plans", 0) == 0:
        notes.append("swing produced ZERO trade plans")
    if i.get("universe", 0) == 0:
        notes.append("intraday universe is EMPTY")
    if notes:
        return False, "; ".join(notes)
    return True, (f"swing {s.get('positions')} pos / {s.get('plans')} plans · "
                  f"intraday {i.get('universe')} universe / "
                  f"{i.get('takeable', 0)} takeable")


def check_learning_loop() -> tuple[bool, str]:
    """
    Is the learning loop actually being fed?

    THE FAILURE THIS EXISTS TO CATCH IS SILENT BY CONSTRUCTION.

    intraday_setups records every detection with its verdict, and outcomes are
    resolved after the close by the daemon's shutdown path — and only there. A
    day the daemon crashed, was killed, or never held the lease is simply never
    scored, and nothing revisits it. On 31 July the table held 460 detections of
    which 241 — every setup from 28 and 29 July — had a cost_verdict and a NULL
    outcome, permanently.

    Nothing reported that. weekly_review needs 20 resolved outcomes before it
    will judge an engine, so a starved loop does not announce breakage; it
    announces "only 8 outcomes, below the 20 needed to judge", which reads like
    patience. Meanwhile the conviction floors, the lifecycle states and the gate
    calibration all rest on evidence that is quietly missing.

    Today is excluded — its setups are legitimately unresolved until the close.
    """
    from config import get_supabase
    from intraday import outcomes
    sb = get_supabase()
    pending = outcomes.unresolved_days(sb, days=30)
    if not pending:
        # count='exact' is a HEADER, not a row fetch. The previous form was
        # `.limit(1000)` and reported len() of what came back, so this line
        # would have read "1000 resolved outcomes on hand" forever once the
        # table passed a thousand rows — a number that stops moving is a
        # number nobody can use to notice anything.
        n = (sb.table("intraday_setups").select("id", count="exact")
               .not_.is_("outcome", "null").limit(1).execute().count)
        return True, f"every past session scored ({n} resolved outcomes on hand)"
    total = sum(n for _, n in pending)
    days = ", ".join(d for d, _ in pending[:4])
    return False, (f"{total} detection(s) across {len(pending)} past session(s) "
                   f"were never scored ({days}) — the weekly review is judging "
                   f"engines on a fraction of the evidence. Fix: "
                   f"python -m intraday.outcomes --backfill")


def check_cost_rates() -> tuple[bool, str]:
    """
    Do the configured charge rates still match the published schedule?

    Rates live in system_config so they can be corrected without a deploy. The
    cost of that flexibility is that a rate can go stale and nothing notices —
    and cost_model's own docstring says a stale rate "silently biases every
    sizing decision", which is exactly what happened: cost_exchange_pct sat at
    the superseded NSE 0.00297% after NSE moved to 0.00307%.

    PUBLISHED is the Zerodha equity schedule, NSE, pinned here as the reference
    the config is checked against. It does NOT detect Zerodha changing its
    prices — nothing automated can — but it does detect the config drifting
    from what this file says the schedule is, which is the failure that
    actually occurred. When Zerodha revises a rate, change it here AND in
    system_config; the check failing is the reminder that both must move.
    """
    from intraday.cost_model import _rates
    PUBLISHED = {
        # equity DELIVERY (CNC)
        "CNC": {"brokerage_flat": 0.0, "brokerage_pct": 0.0,
                "stt_buy_pct": 0.1, "stt_sell_pct": 0.1,
                "exchange_pct": 0.00307, "sebi_pct": 0.0001,
                "stamp_buy_pct": 0.015, "gst_pct": 18.0},
        # equity INTRADAY (MIS)
        "MIS": {"brokerage_flat": 20.0, "brokerage_pct": 0.03,
                "stt_buy_pct": 0.0, "stt_sell_pct": 0.025,
                "exchange_pct": 0.00307, "sebi_pct": 0.0001,
                "stamp_buy_pct": 0.003, "gst_pct": 18.0},
    }
    bad = []
    for product, expected in PUBLISHED.items():
        got = _rates(product)
        for key, want in expected.items():
            if abs(float(got.get(key, 0)) - want) > 1e-9:
                bad.append(f"{product}.{key}={got.get(key)} (published {want})")
    if bad:
        return False, ("charge rates disagree with the published schedule: "
                       + "; ".join(bad))
    # DP is not in the rate table above because it is account-specific and only
    # the ledger proves it. This one comes from the operator's own ledger rows.
    dp = _rates("CNC")["dp_per_sell"]
    if dp <= 0:
        return False, ("CNC dp_per_sell is 0 — delivery sells pay a flat "
                       "depository fee and omitting it understates every swing exit")
    return True, (f"{sum(len(v) for v in PUBLISHED.values())} rates match the "
                  f"published NSE equity schedule, CNC DP Rs {dp:.2f}")


def check_exit_actions() -> tuple[bool, str]:
    """
    Every exit action that can SELL must be able to sell from either caller,
    and must be able to alert.

    Three lists decide what an exit action does, in three files:
      · position_lifecycle.manage_open_positions  places the order (pipeline)
      · intraday.engine._auto_exit                places the order (daemon)
      · position_lifecycle.send_action_alerts     tells the operator

    They are independent literals, so adding a rule to one and forgetting the
    others is a silent, one-line mistake — and it happened: EXIT_GIVEBACK and
    EXIT_STALL were added to the pipeline's list only, so the daemon would not
    act on them and nothing would have alerted. Worst case that is a position
    sold with no notification; best case a loss-cutting rule that never fires.

    This compares the three sets by parsing the source, so it fails the moment
    they drift rather than the next time money moves.
    """
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    def literal_after(path: Path, anchor: str) -> set:
        src = path.read_text(encoding="utf-8")
        i = src.find(anchor)
        if i < 0:
            return set()
        seg = src[i:i + 700]
        return set(re.findall(r'"(EXIT_[A-Z_]+|BOOK_PARTIAL)"', seg))

    pipe  = literal_after(root / "control/position_lifecycle.py",
                          'if act in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME",\n'
                          '                   "EXIT_DETERIORATION"')
    alert = literal_after(root / "control/position_lifecycle.py", "SELLABLE = (")
    daemn = literal_after(root / "intraday/engine.py", "if action not in (\"EXIT_STOP\"")

    if not (pipe and alert and daemn):
        return False, ("could not locate one of the three exit-action lists — "
                       "the check itself is broken, fix it before trusting it")

    # The daemon carries two intraday-only actions the swing pipeline never
    # emits. Everything else must match exactly.
    intraday_only = {"EXIT_INVALIDATED", "EXIT_SQUAREOFF"}
    d = daemn - intraday_only
    if d == pipe == alert:
        return True, f"{len(d)} sell-capable actions agree across pipeline, daemon and alerts"

    return False, (f"exit-action lists disagree — pipeline={sorted(pipe)} "
                   f"daemon={sorted(d)} alerts={sorted(alert)}; "
                   f"missing from daemon: {sorted(pipe - d) or 'none'}, "
                   f"missing from alerts: {sorted(pipe - alert) or 'none'}")


def storage_snapshot() -> dict:
    """
    Where the database stands against its plan ceiling, and when it breaches.

    Shared by the health check and by anything that needs the same numbers, so
    there is one measurement rather than two that can disagree.

    GROWTH IS MEASURED, NOT ASSUMED. Migration 016 sized stock_data_daily at
    "roughly 190 MB" from 48,967 rows; it is 58 MB at 53,463. A projection
    resting on that estimate would have declared an emergency two years early
    and been ignored the day it was real. So growth is derived here from rows
    actually added in the last 30 days, at the table's own measured bytes/row.

    Only tables above 1% of the ceiling are projected. They are 84% of the
    database and the tail cannot move the date; the coverage figure is reported
    so a shrinking coverage number is visible rather than silent.
    """
    import datetime as _dt
    from config import get_supabase, cfg_float

    sb       = get_supabase()
    ceiling  = cfg_float("storage_ceiling_mb", 500.0) * 1024 * 1024
    fail_pct = cfg_float("storage_fail_pct", 80.0)

    # sort-exempt: v_storage_usage is one row per public table — 56 rows,
    # measured 15-Aug-2026. It is a catalogue view, not a data table, and
    # cannot approach the cap without the schema itself having 1000 tables.
    # It also has no `id`; the natural key is table_name.
    rows, off = [], 0
    while True:
        page = (sb.table("v_storage_usage")
                  .select("table_name,total_bytes")
                  .range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000

    total = sum(r["total_bytes"] or 0 for r in rows)
    rows.sort(key=lambda r: -(r["total_bytes"] or 0))

    # Whichever of these a table has, first match wins. A table with none of
    # them is not append-only in any way this can measure, so it is skipped and
    # its bytes are excluded from the coverage figure rather than assumed flat.
    date_cols = ("date", "snapshot_date", "news_date", "trade_date",
                 "entry_date", "created_at", "ingested_at")
    since = str(_dt.date.today() - _dt.timedelta(days=30))

    bytes_per_day, covered, unmeasured = 0.0, 0, []
    for r in rows:
        size = r["total_bytes"] or 0
        if size < ceiling * 0.01:
            break                              # sorted desc — the rest are smaller
        name = r["table_name"]
        for col in date_cols:
            try:
                n_all = sb.table(name).select(col, count="exact").limit(1).execute().count
                n_30  = (sb.table(name).select(col, count="exact")
                           .gte(col, since).limit(1).execute().count)
                break
            except Exception:
                continue
        else:
            unmeasured.append(name)
            continue
        if not n_all:
            unmeasured.append(name)
            continue
        bytes_per_day += (n_30 or 0) * (size / n_all) / 30.0
        covered += size

    # "already passed" and "never at this growth rate" are different facts and
    # printing both as "never" is how a breached ceiling reads like headroom.
    def _breach(limit_bytes: float) -> str:
        if limit_bytes <= total:
            return "ALREADY PASSED"
        if bytes_per_day <= 0:
            return "not projectable"
        d = _dt.date.today() + _dt.timedelta(days=(limit_bytes - total) / bytes_per_day)
        return d.isoformat()

    return {
        "total_bytes":   total,
        "ceiling_bytes": ceiling,
        "pct":           100.0 * total / ceiling if ceiling else 0.0,
        "fail_pct":      fail_pct,
        "mb_per_month":  bytes_per_day * 30 / 1024 / 1024,
        "coverage_pct":  100.0 * covered / total if total else 0.0,
        "unmeasured":    unmeasured,
        "fail_date":     _breach(ceiling * fail_pct / 100.0),
        "full_date":     _breach(ceiling),
        "top":           [(r["table_name"], r["total_bytes"] or 0) for r in rows[:12]],
    }


def check_storage() -> tuple[bool, str]:
    """
    Is the database going to stop accepting writes, and when?

    THIS FAILS. It does not warn.

    Breaching the ceiling puts the project into read-only mode: writes fail,
    the evening pipeline produces no signals, on an ordinary Tuesday, with no
    other symptom. It is the only failure in this system that is a total loss
    rather than a bad trade, and a WARN on a list of ten green lines is a line
    nobody reads.

    Failing at 80% rather than 100% is deliberate — a migration run against a
    near-full database can itself fail, so the guard has to fire while there is
    still room to fix it.

    Both thresholds are config (storage_ceiling_mb, storage_fail_pct) so the
    guard can be demonstrated failing without waiting for the disk to fill.
    """
    s = storage_snapshot()
    mb, ceil_mb = s["total_bytes"] / 1048576, s["ceiling_bytes"] / 1048576

    if s["mb_per_month"] > 0:
        when = (f"{s['mb_per_month']:.0f} MB/month → {s['fail_pct']:.0f}% on "
                f"{s['fail_date']}, full on {s['full_date']}")
    else:
        when = "no rows added in the last 30 days — growth not projectable"

    detail = (f"{mb:.0f} MB of {ceil_mb:.0f} MB ({s['pct']:.1f}%), {when} "
              f"[growth measured across {s['coverage_pct']:.0f}% of size]")

    if s["pct"] >= s["fail_pct"]:
        biggest = ", ".join(f"{n} {b/1048576:.0f}MB" for n, b in s["top"][:3])
        return False, (f"STORAGE CEILING: {detail}. Largest: {biggest}. "
                       f"Writes fail at the ceiling — the evening pipeline "
                       f"produces no signals. Roll off history or raise the plan.")
    return True, detail


def check_feed_integrity() -> tuple[bool, str]:
    """
    Two properties the decision path depends on and cannot detect losing.

    1. THE TICK HANDLER DOES NO I/O.
       on_ticks runs on the websocket thread for every tick of ~95 symbols. One
       logger call or one database write in there backs the socket up behind it,
       and prices go late in exactly the fast market where lateness costs money.
       Nothing about that failure looks like a failure — the daemon runs, the
       numbers move, they are just behind.

    2. THE STALENESS GUARD IS WIRED TO THE ENTRY PATH.
       A dead socket with a live cache keeps serving the reference levels from
       whenever it died. The guard exists so no entry is decided on data of
       unknown age; a guard that is present but not consulted is worse than
       none, because it reads as protection.

    Asserted by reading the source, the same way check_exit_actions asserts its
    three lists still agree — so it fails the moment someone removes either,
    rather than the next time the market is fast.
    """
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    src = (root / "intraday/price_feed.py").read_text(encoding="utf-8")
    i = src.find("def on_ticks(")
    if i < 0:
        return False, "cannot find on_ticks in price_feed — the check itself is broken"
    # The handler body ends where the next nested def begins.
    j = src.find("def on_connect(", i)
    body = src[i:j if j > i else i + 2000]
    # Strip comments before looking for calls: this file explains at length why
    # the handler must not log, and the word appears in that explanation.
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    dirty = [p for p in ("logger.", ".execute(", "sb.table", "requests.", "print(")
             if p in code]
    if dirty:
        return False, (f"tick handler performs I/O ({', '.join(dirty)}) — this runs "
                       f"per tick on the socket thread and will make prices late")

    eng = (root / "intraday/engine.py").read_text(encoding="utf-8")
    if not re.search(r"stale\s*=\s*self\.stale_contexts\(\)", eng):
        return False, ("the staleness guard is not called in the entry path — "
                       "a dead feed with a live cache would trade on whatever it "
                       "last saw")
    if "if sym in stale:" not in eng:
        return False, ("stale_contexts() is computed but its result never filters "
                       "a symbol — the guard is decorative")

    from config import cfg_float, cfg_bool
    age  = cfg_float("intraday_context_max_age_s", 420.0)
    quote_on = (cfg_bool("intraday_quote_mode_range", False)
                or cfg_bool("intraday_quote_mode_vwap", False))
    mode = "QUOTE" if quote_on else "LTP"
    return True, (f"tick handler is I/O-free, staleness guard active at {age:.0f}s, "
                  f"feed mode {mode}")


def check_quote_parity() -> tuple[bool, str]:
    """
    If quote mode is live, is anyone still checking it agrees with reality?

    08-Aug-2026. tools/quote_parity.py's original guidance was: arm logging,
    run one session, and if it read clean, enable the switch and disarm the
    logging. That is a verdict frozen on whatever ONE session's market did —
    a quiet day and a fast one do not necessarily produce the same
    day_high/day_low or vwap agreement, and disarming meant nothing was left
    running to notice if a later session disagreed. This is the check that
    closes that gap: it fails if a switch is on but nobody is watching, or if
    RANGE (day_high/day_low, clean at baseline) has actually regressed.

    VWAP AND PREV_CLOSE FAULTING IS NOT, BY ITSELF, A FAILURE HERE. Both were
    already measured FAULT on 07-Aug and both switches were kept ON anyway —
    vwap because the gap is a structural formula difference, not staleness;
    prev_close because the live side is more likely correcting a
    stock_data_daily bug than causing one (see
    intraday/engine.py::apply_live_quotes()'s docstring). Failing this check
    every single day on an accepted, expected condition is the exact "check
    that can never pass" trap CLAUDE.md warns about — it gets read once,
    ignored forever, and stops meaning anything. So both are reported in the
    detail string for visibility, never as the reason this returns False.
    RANGE is different: it measured clean, so any fault there is new
    information, not a re-statement of what was already known and accepted.

    17-Aug-2026 — WHAT "CLEAN AT BASELINE" WAS ACTUALLY MEASURING. This fired
    RANGE REGRESSED on 19 of 402 comparisons. Both halves of that sentence
    were wrong.

    The 402 was a silently truncated read: the select below had no paging, and
    PostgREST caps a response at 1000 rows without saying so. Five days of
    parity logging is ~190,000 rows, so the check was judging a regression on
    an arbitrary, unordered 0.5% sample of the window it claimed to cover. It
    now pages through `fetch_all`.

    The baseline was worse. 07-Aug read clean because the daemon started at
    10:08 that day — and 10-Aug (09:41), 11-Aug (09:30) and 13-Aug (09:52)
    also read clean, while every day whose first sample was at 09:21 (12-Aug,
    14-Aug, 17-Aug) read FAULT. The fault is not a feed regression; it is a
    pre-open artifact that a late start never sees, and the "clean" baseline
    this check was defending was a measurement taken an hour after the window
    where the defect lives. See intraday/bar_builder.py's SESSION_OPEN.
    """
    from config import cfg_bool, fetch_all, get_supabase, today_ist
    from datetime import timedelta

    range_on = cfg_bool("intraday_quote_mode_range", False)
    vwap_on  = cfg_bool("intraday_quote_mode_vwap", False)
    if not (range_on or vwap_on):
        return True, "quote mode is off — nothing live to verify"

    on_names = "+".join(n for n, on in (("range", range_on), ("vwap", vwap_on)) if on)
    if not cfg_bool("intraday_quote_parity_log", False):
        return False, (f"intraday_quote_mode_{on_names} is ON but intraday_quote_parity_log "
                       f"is OFF — no ongoing check that today's feed still agrees with the "
                       f"historical endpoint; today's numbers could differ from whatever "
                       f"session this was last verified against")

    sb = get_supabase()
    cutoff = (today_ist() - timedelta(days=5)).isoformat()
    # THIS READ WAS UNPAGED, AND IT INVERTED THE VERDICT.
    #
    # `intraday_quote_parity` is 178,545 rows; this 5-day window is 167,025 of
    # them. Unpaged, PostgREST returned 1000 — 0.6% — with no error. Measured
    # 15-Aug-2026, both verdicts computed from the same cutoff:
    #
    #   truncated (1000 rows):  True  — "400 day_high/day_low comparisons, all clean"
    #   complete (100,215):     False — "176 of 66810 day_high/day_low comparisons behind"
    #
    # So the check reported RANGE clean while RANGE had regressed, and
    # `intraday_quote_mode_range` has been trusted on that all-clear. This is
    # the project's own "a check that cannot fail is not a check" landmine: it
    # could fail in principle, but never on the evidence it was given.
    #
    # Filtered to the three fields the verdicts actually read (the other
    # ~66,810 rows were fetched and discarded in Python) and paged. Costs ~8s.
    # A health check that takes eight seconds and tells the truth is worth more
    # than an instant one that says what you hoped.
    from config import fetch_all
    try:
        # PAGED, AND ORDERED WHILE PAGING. An unpaged select stops at 1000
        # rows with no error; unordered LIMIT/OFFSET paging is worse still and
        # returns duplicates in place of rows it never fetched. See
        # config.fetch_all's docstring for the measurement.
        #
        # FILTERED TO THE THREE FIELDS THE VERDICTS ACTUALLY READ, SERVER
        # SIDE — from diagnostic/rescore-complete-prices, merged 18-Aug-2026.
        # range_verdict/vwap_verdict only ever look at day_high/day_low/vwap;
        # everything else (prev_close, volume — measured at ~66,810 of the
        # ~178,545-row table on 15-Aug) was being fetched and discarded in
        # Python. This table is the single largest paged read in the
        # codebase, so pushing the filter to the database instead of the
        # client is the difference between an 8-second check and one that
        # takes much longer for no better an answer.
        rows = fetch_all(lambda: sb.table("intraday_quote_parity")
                                   .select("field,diff_pct,ts")
                                   .gte("ts", cutoff)
                                   .in_("field", ["day_high", "day_low", "vwap"]))
    except Exception as e:
        return False, f"could not read intraday_quote_parity: {e}"

    if not rows:
        return False, (f"intraday_quote_mode_{on_names} is ON and logging is armed, but no "
                       f"comparisons in the last 5 days — armed does not mean collecting; "
                       f"confirm the daemon actually picked up the config change")

    from tools.quote_parity import range_verdict, vwap_verdict
    # RANGE is the only one that can fail this check — it measured clean at
    # baseline, so a fault now is new. VWAP is reported but never blocking —
    # see the docstring for why failing on an already-accepted condition
    # would make this check impossible to ever pass.
    notes = []
    if range_on:
        ok, detail = range_verdict(rows)
        if ok is False:
            return False, (f"RANGE FAULT — {detail}. The live side leads by construction "
                           f"(the socket's day range is the exchange's; the bar side is up "
                           f"to 300s behind it), so a live value BEHIND the bar side means "
                           f"the bar side holds a price today never traded at. Do NOT turn "
                           f"intraday_quote_mode_range off — that switch is what keeps the "
                           f"bad number out of the engines. Run "
                           f"`python -m tools.quote_parity` and check what the fetched "
                           f"value equals; when it equals the previous close, the bar "
                           f"series has picked up an out-of-session print.")
        notes.append(f"range: {detail}")
    if vwap_on:
        _, detail = vwap_verdict(rows)
        notes.append(f"vwap: {detail}")

    return True, "checked against last 5 days: " + "; ".join(notes)


def check_governance() -> tuple[bool, str]:
    """
    Is there still exactly one door, and is the conviction layer still annotation?

    THE THING THIS WATCHES CANNOT ANNOUNCE ITSELF.

    Auto-apply was removed in code rather than switched off, precisely so that
    re-opening it would require an edit somebody has to justify. But an edit is
    exactly what could happen — a merge conflict resolved the wrong way, a
    refactor that "cleaned up" an early return, a revert. Nothing about a
    reopened door looks wrong: proposals simply start applying themselves, which
    is what the system did for months and reported as success.

    Likewise the conviction weights. They are zero because no tier-by-tier
    forward return exists yet; a non-zero value means an unmeasured component is
    back at the top of the decision stack deciding where capital goes.
    """
    from config import cfg_float, cfg_bool
    from swing.brain.backtester_and_change_manager import evaluate_auto_apply

    # Behavioural, not textual: hand it the exact proposal shape that WOULD have
    # auto-applied under the old live policy and require a refusal.
    ok, why = evaluate_auto_apply({
        "proposal_type": "THRESHOLD_CHANGE", "confidence": 0.99,
        "backtest_result": {"wr_delta": 25.0, "high_impact": False},
    })
    if ok:
        return False, ("AUTO-APPLY IS OPEN AGAIN — a threshold can move with nobody "
                       f"reading it ({why}). Every parameter change must go through "
                       f"the proposal queue and a human.")

    bad = [f"{k}={v}" for k, v in (("rank_weight_tier", cfg_float("rank_weight_tier", 0.0)),
                                   ("rank_weight_conviction", cfg_float("rank_weight_conviction", 0.0)))
           if v]
    if bad:
        return False, (f"conviction is back in the ranking ({', '.join(bad)}) but no "
                       f"tier-by-tier forward returns exist yet — an unmeasured "
                       f"component is deciding where capital goes")

    freeze = "on" if cfg_bool("governance_freeze_enabled", False) else "OFF"
    oos    = "on" if cfg_bool("governance_require_oos", False) else "OFF"
    return True, (f"one door: auto-apply refuses, conviction is annotation, "
                  f"freeze {freeze}, out-of-sample {oos}")


def check_allocator_isolation() -> tuple[bool, str]:
    """
    Can the allocator reach an order path? It must not be able to.

    THIS IS THE GUARD THAT MAKES SHADOW MODE SAFE ON A LIVE BOOK.

    Every other protection around the allocator is a value in a config row —
    switches default off, shadow defaults true — and a config row can be
    changed by anyone, including by a script, including by accident. The
    prohibition on `allocation` importing `execution` is different in kind: a
    module that cannot import the thing that places orders cannot place one
    however wrong its arithmetic, however confused its priors, however badly
    someone wires its call-site.

    So it is asserted rather than assumed, by inspection, the same way the exit
    lists and the tick handler are. It fails the moment somebody adds the import
    that would make it convenient.
    """
    import re
    from pathlib import Path
    pkg = Path(__file__).resolve().parent.parent / "allocation"
    if not pkg.is_dir():
        return True, "allocation package not built yet"

    banned = ("execution", "order_manager", "paper_broker", "gtt_manager", "kite")
    offenders = []
    for f in sorted(pkg.glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if not re.match(r"\s*(import|from)\s", code):
                continue
            if any(b in code for b in banned):
                offenders.append(f"{f.name}:{n}")
    if offenders:
        return False, (f"allocation imports an order path at {', '.join(offenders)} — "
                       f"shadow mode is NO LONGER SAFE on a live book, because the "
                       f"allocator can now reach execution regardless of its switches")

    # THE SWITCH MUST BE CONSUMED, NOT MERELY SET.
    #
    # alloc_live_* originally appeared in exactly one place — writing a column
    # value into allocation_decisions — and nothing read it. Flipping it would
    # have produced a system REPORTING itself as live-allocating while the
    # greedy path still chose every entry. That is this project's signature
    # failure and it had already happened four times before this one.
    #
    # Worse, the first attempt to fix it wrote the swing veto into a function
    # where the edit silently did not apply, so the switch was half-wired: live
    # on intraday, inert on swing, and nothing said so. Both books are therefore
    # asserted independently, by locating the actual call site rather than
    # trusting that the function exists.
    eng = (pkg.parent / "intraday/engine.py").read_text(encoding="utf-8")
    if "def allocator_permits" not in eng:
        return False, ("allocator_permits() is gone — alloc_live_* would set a "
                       "database column and gate nothing, and the allocator would "
                       "report itself live while the greedy path chose every entry")
    missing = [book for book, call in
               (("intraday", 'allocator_permits(st.symbol, "MIS", "INTRADAY")'),
                ("swing",    'allocator_permits(sym, "CNC", "SWING")'))
               if call not in eng]
    if missing:
        return False, (f"the allocator veto is not wired for {', '.join(missing)} — "
                       f"alloc_live_{missing[0]} would be set and consumed by "
                       f"nothing, which is a switch that lies about what it does")

    from config import cfg_bool
    live = [b for b in ("intraday", "swing") if cfg_bool(f"alloc_live_{b}", False)]
    mode = f"LIVE for {'+'.join(live)}" if live else "shadow only"
    return True, (f"allocation cannot import an order path; veto wired for both "
                  f"books ({mode})")


def check_allocator_hurdle() -> tuple[bool, str]:
    """
    Can the bar ever be cleared, and does an allocator with no data stand aside?

    A CHECK THAT CANNOT PASS IS AS USELESS AS ONE THAT CANNOT FAIL, and on
    05-Aug-2026 this system ran an entire session on one. `scoring.score()`
    returns net-of-cost expected R per day; the hurdle was built from gross
    realised R per trade. Different quantities, compared with `<`. Every
    intraday setup was declined, all day, and nothing anywhere said the bar was
    unclearable — the logs read like a market with no opportunities.

    Three assertions, all behavioural rather than textual:

      1. THE BAR AND THE EDGE COME FROM ONE DEFINITION. _empirical_base must
         read the same column `_record` writes `score()`'s output into. Units
         cannot drift when there is only one place the quantity is defined.
      2. A COLD START MUST BE PERMISSIVE. With no history the allocator has no
         opinion, and an allocator with no opinion must be indistinguishable
         from no allocator — otherwise its first day in production is a
         shutdown. Tested by actually scoring a proposal against a cold bar.
      3. SLOTS ARE PER BOOK. Asserted at the call site by name, because the
         pooled version silently capped the intraday book at the swing book's
         morning.
    """
    import inspect
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent
    pkg = root / "allocation"
    if not pkg.exists():
        return True, "allocation package not built yet"

    from allocation import hurdle as H
    from allocation import allocator as A
    from allocation import policies as P

    # 1 — one definition of the quantity, read by both sides.
    #
    # MATCHED AGAINST CODE, NOT PROSE. The first version of this check tested
    # `"allocation_decisions" in source` and passed happily after the table was
    # switched back to intraday_setups, because the docstring underneath still
    # named the right one. An assertion a comment can satisfy is decoration.
    # The docstring is stripped and the actual call expression is required.
    def _code(fn) -> str:
        src = inspect.getsource(fn)
        doc = inspect.getdoc(fn)
        if doc:
            for line in doc.splitlines():
                src = src.replace(line, "")
        return src

    base_src = _code(H._empirical_base)
    rec_src  = _code(A.Allocator._record)
    reads = re.findall(r'\.table\(\s*["\'](\w+)["\']', base_src)
    if reads != ["allocation_decisions"]:
        return False, (f"the hurdle reads its arrival distribution from "
                       f"{reads or 'nothing'} rather than allocation_decisions — "
                       f"it is being built from a different population than the one "
                       f"scoring.score() writes into, which is how a bar denominated "
                       f"in gross R per trade came to be compared against an edge "
                       f"denominated in net R per day, and emptied the intraday book "
                       f"for a full session")
    if 'float(r["edge"])' not in base_src:
        return False, ("the hurdle no longer reads the `edge` column itself — the "
                       "bar and the proposals it judges must come from one "
                       "definition of the quantity or they will drift apart again")
    if '"edge": v.get("edge")' not in rec_src:
        return False, ("allocation_decisions.edge is no longer written from the "
                       "scorer's own output, so the hurdle's population and the "
                       "proposals it judges are no longer the same quantity")

    # 2 — the cold start must admit a proposal, not refuse one.
    #     Exercised through the real hurdle and the real policy.
    bar, inputs = H._cold_start(0, 40, "health check")
    if bar != float("-inf") and bar > -1.0:
        return False, (f"the cold-start bar is {bar}, which REFUSES a proposal whose "
                       f"expected R merely fails to exceed it. With no history the "
                       f"allocator must stand aside, not stand down — a bar of 0.0 "
                       f"against a cost-netted edge declines every intraday setup, "
                       f"because the measured prior (+0.08R) does not cover the MIS "
                       f"round trip (+0.21R)")
    probe = {"symbol": "HEALTHCHECK", "proposal": None, "edge": -0.13}
    v = P.intraday_stopping([probe], bar, 1)[0]
    if v["verdict"] != "TAKE":
        return False, (f"a typical intraday setup (edge -0.13) is {v['verdict']} "
                       f"against the cold-start bar {bar} — this is exactly the "
                       f"state that took the intraday book to zero trades. "
                       f"{v.get('reason')}")

    # 3 — per-book slots, asserted at the call site.
    eng = (root / "intraday/engine.py").read_text(encoding="utf-8")
    if "slots_by_framework" not in eng:
        return False, ("the allocator is still being given one pooled slot count "
                       "for both books — a single swing entry then caps the "
                       "intraday book, whose own governance allows "
                       "intraday_max_new_per_day, at one for the rest of the day")

    # 4 — regime_bucket() must be reachable by the vocabulary it is ACTUALLY
    #     called with, not only by the one its docstring was written against.
    #
    # 05-Aug-2026: this function was written to match the swing regime engine's
    # SPACE-separated states ("RISK ON") but its only live caller
    # (_allocate_shadow, via regime=mc.state) has always fed it the intraday
    # market context's UNDERSCORE-separated states ("RISK_ON"). "RISK ON" in
    # "RISK_ON" is False, so STRONG was unreachable from production for as long
    # as this function existed — every bar was drawn from the WEAK bucket only,
    # silently defeating the segmentation this module exists to provide.
    #
    # Exercised through the real function against the real value, not read from
    # source: a check that only greps for a substring can be satisfied by a
    # comment, same lesson as check #1 above.
    if H.regime_bucket("RISK_ON") != "STRONG":
        return False, (
            "regime_bucket('RISK_ON') is not STRONG — the intraday market "
            "context's own favourable state cannot reach the STRONG bucket. "
            "This is the exact production call (_allocate_shadow passes "
            "regime=mc.state, which is always the underscore-separated "
            "vocabulary), so if this fails, STRONG is unreachable and the "
            "hurdle's regime segmentation is silently pooling everything into "
            "WEAK — the same failure shape as the units bug this check's "
            "assertion #1 exists to catch, one level up")
    if H.regime_bucket("TRENDING") != "STRONG":
        return False, (
            "regime_bucket('TRENDING') is not STRONG — the swing regime "
            "engine's OWN STRONGEST STATE does not clear its own bucket "
            "boundary. If this is failing, whatever match this function uses "
            "is narrower than the vocabulary it is written against")

    # 5 — the TIME half of the hurdle must actually vary with time.
    #
    # `hurdle()` has two terms: scarcity (fewer slots -> higher bar) and time
    # (more session left -> higher bar, because better is probably still
    # coming). The time term reads `minutes_left`, which _allocate_shadow
    # sourced from `getattr(st, "minutes_to_close", 0)` — an attribute
    # SessionState has never had. The getattr default was taken on every call
    # since the allocator was wired in, so minutes_left was permanently 0,
    # time_mult permanently 1.0, and half the hurdle's stated purpose has never
    # operated. Scarcity still moved the bar, which is precisely why nothing
    # looked broken.
    #
    # A marker grep cannot catch this and did not: the call site reads
    # `minutes_left=...` and looks entirely correct. So this probes the NUMBER
    # at two instants and requires it to differ — the same reasoning as
    # assertion 4, which exercises regime_bucket rather than grepping for it.
    _bar_early, _in_early = H.hurdle("STRONG", 4, 355, "INTRADAY", None, max_slots=4)
    _bar_late,  _in_late  = H.hurdle("STRONG", 4, 20,  "INTRADAY", None, max_slots=4)
    if _in_early["time_mult"] <= _in_late["time_mult"]:
        return False, (
            f"the hurdle's TIME term does not vary with time — time_mult is "
            f"{_in_early['time_mult']} with a full session left and "
            f"{_in_late['time_mult']} with 20 minutes to go. It must be HIGHER "
            f"early, because an entry spent at 09:20 forecloses five hours of "
            f"arrivals. If these are equal the caller is passing a constant, "
            f"which is what a missing SessionState attribute silently produces")
    # CODE ONLY, NOT COMMENTS. The first version of this grep matched the
    # comment in engine.py that documents the bad pattern in order to warn
    # against it — so a correctly-fixed file failed its own check. The mirror
    # of assertion 1's lesson: an assertion a comment can satisfy is
    # decoration, and one a comment can falsely TRIP is worse, because it
    # trains the reader to ignore a red line.
    eng_code = "\n".join(
        l.split("#", 1)[0]
        for l in (root / "intraday/engine.py").read_text(encoding="utf-8").splitlines())
    if 'getattr(st, "minutes_to_close"' in eng_code:
        return False, (
            "intraday/engine.py still reads getattr(st, \"minutes_to_close\") — "
            "SessionState has no such field (it is minutes_to_squareoff; "
            "minutes_to_close is a module FUNCTION), so the default is taken "
            "every call and the allocator is told the session has 0 minutes "
            "left. Same defect that made every short's runway -10 minutes")

    from config import cfg_bool, cfg_int, cfg_float

    # 7 — A BAR THAT IS TOO LOW IS THE SAME DEFECT AS ONE THAT IS TOO HIGH.
    #
    # Everything above this point asks whether the bar can be CLEARED. Nothing
    # asked whether it can be cleared by a trade that loses money, and on
    # 10-Aug-2026 it was: the INTRADAY STRONG bucket's bar settled at -1.09359,
    # DEVYANI/SDN cleared it at edge -1.0935 — by 0.00009 — and closed 99
    # seconds later at -0.813R. Across that bucket, DECLINEd proposals averaged
    # -1.0937 and TAKEn ones -1.0935: two ten-thousandths of separation across
    # 142 proposals, which is not a decision, it is a coin flip on rounding.
    #
    # This project's own paired rule: a check that cannot fail is not a check,
    # and a check that cannot pass is the same defect wearing a different hat.
    # This check had only ever been written for one of those two directions.
    floor_edge = cfg_float("alloc_edge_absolute_floor", 0.0)
    _bar_neg, _in_neg = H.hurdle("STRONG", 4, 20, "INTRADAY", None, max_slots=4)
    if _bar_neg != float("-inf") and _bar_neg < floor_edge:
        return False, (
            f"the live INTRADAY bar is {_bar_neg:.5f}, below the absolute floor "
            f"{floor_edge}. `edge` is expected R NET of the round trip, so a "
            f"negative bar admits proposals the scorer itself has measured as "
            f"losing money — and because the bar DECAYS toward its base as the "
            f"session runs out, the system gets MORE willing to take them the "
            f"less time they have to work. Cold starts are exempt (no opinion "
            f"is not the same claim as measured-bad); this is not a cold start")

    # 8 — the floor must actually be reachable by the code path, not just
    #     configured. Probed through the real hurdle on a synthetic all-negative
    #     population, because assertion 7 passes trivially on a day when the
    #     live population happens to be healthy.
    class _NegSB:
        """An arrival population that is entirely negative — the exact shape
        INTRADAY had on 10-Aug-2026."""
        def table(self, name):
            rows = [{"edge": -1.0 - i * 0.001, "framework": "INTRADAY",
                     "regime_bucket": "STRONG", "symbol": f"S{i}",
                     "trade_date": "2026-08-01"} for i in range(200)]
            return _StubQuery(rows)

    _bar_probe, _in_probe = H.hurdle("STRONG", 4, 20, "INTRADAY", _NegSB(), max_slots=4)
    if _bar_probe < floor_edge:
        return False, (
            f"against an all-negative arrival population the bar came back "
            f"{_bar_probe:.5f}, under the floor {floor_edge} — the absolute "
            f"floor is not being applied. This is the 10-Aug DEVYANI path: a "
            f"percentile of a losing distribution is still a losing number, and "
            f"without this clamp the allocator ranks losses against each other "
            f"and calls the least-bad one a TAKE")
    if not _in_probe.get("absolute_floor_applied"):
        return False, (
            "the bar cleared the floor on an all-negative population without "
            "`absolute_floor_applied` being set — the verdict would record no "
            "trace of the clamp, and a decision that cannot be reconstructed "
            "is a defect (§19)")

    live = [b for b in ("intraday", "swing") if cfg_bool(f"alloc_live_{b}", False)]
    return True, (f"bar and edge share one definition; a cold start admits a "
                  f"typical setup; slots are per book "
                  f"(swing {cfg_int('swing_max_new_per_day', 2)}, "
                  f"intraday {cfg_int('intraday_max_new_per_day', 4)}); "
                  f"time term varies ({_in_early['time_mult']} early vs "
                  f"{_in_late['time_mult']} late); "
                  f"bar floored at {floor_edge} so a measured-losing proposal "
                  f"cannot clear it"
                  + (f"; veto LIVE for {'+'.join(live)}" if live else "; shadow only"))


class _StubQuery:
    """Minimal PostgREST-shaped stub for check_allocator_hurdle's probe."""
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self): return self

    def order(self, col, *a, **k):
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        Without this the probe raised AttributeError inside hurdle()'s own
        `except`, which falls back to the cold-start bar — so this check went
        red reporting "-inf, under the floor", i.e. a symptom three steps
        downstream of a missing stub method. It caught the breakage, which is
        the check working; the message just pointed at the clamp rather than
        at the read that never happened.
        """
        return self

    def range(self, start, end):
        return _StubExec(self._rows[start:end + 1])


class _StubExec:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


def check_framework_isolation() -> tuple[bool, str]:
    """
    Can one symbol end up in both books at once?

    The rule "one symbol, one book" was written in two comment blocks and
    implemented in one direction. Intraday skipped any name in `self.positions`;
    swing checked only `_held_by_framework(sym, "SWING")`. So the PAPER book
    refused to collide with the live one, and the LIVE book would buy a name the
    paper book already held — real money layered on a simulated position, with a
    15:15 square-off and a 15-session time stop pointed at the same shares.

    Asserted by locating BOTH call sites by name, the same way the allocator
    veto is asserted — because the previous failure here was not a missing
    function, it was a function that existed and was called from only one side.
    """
    from pathlib import Path
    root = Path(__file__).parent.parent
    eng = (root / "intraday/engine.py").read_text(encoding="utf-8")

    if "def _other_framework_holding" not in eng:
        return False, ("_other_framework_holding() is gone — nothing stops the "
                       "swing book buying a name the intraday book is already "
                       "trading, and the two exit ladders then contradict each "
                       "other on the same shares")
    missing = [book for book, call in
               (("intraday", '_other_framework_holding(sym, "INTRADAY")'),
                ("swing",    '_other_framework_holding(sym, "SWING")'))
               if call not in eng]
    if missing:
        return False, (f"the one-book-per-symbol rule is not enforced on the "
                       f"{', '.join(missing)} side. This is the exact shape of the "
                       f"original defect: the guard existed and only one book "
                       f"called it, so collisions could only be created by the "
                       f"book that was not checking")

    from config import cfg_bool
    if not cfg_bool("one_framework_per_symbol", True):
        return False, ("one_framework_per_symbol is OFF — the swing and intraday "
                       "books may both hold the same name. That is a deliberate "
                       "operator choice, but it is not a state health can call "
                       "healthy: the intraday square-off will sell into a swing "
                       "thesis and the same move is scored twice by the learning loop")

    # THE ASYMMETRIC CARVE-OUT — 06-Aug-2026. INTRADAY may now join a name
    # SWING already holds (same direction only, `intraday_allow_swing_held_symbols`);
    # SWING still refuses unconditionally, unchanged above. Checked separately
    # because the two assertions above would not notice the helper vanishing,
    # its call site being dropped, or its direction guard being silently removed.
    if "def _intraday_may_join_swing_holding" not in eng:
        return False, ("_intraday_may_join_swing_holding() is gone — if the "
                       "INTRADAY call site above still lets a setup through "
                       "after finding a SWING holding, nothing is deciding "
                       "whether that's safe")
    if "_intraday_may_join_swing_holding(other, best.direction)" not in eng:
        return False, ("evaluate_intraday_setups no longer calls "
                       "_intraday_may_join_swing_holding() at the cross-framework "
                       "check — the carve-out logic exists but is disconnected")
    fn_start = eng.index("def _intraday_may_join_swing_holding")
    fn_body = eng[fn_start:fn_start + 1200]
    if "is_short(direction)" not in fn_body:
        return False, ("_intraday_may_join_swing_holding() no longer checks "
                       "direction — a SHORT could join a name SWING holds LONG, "
                       "which contradicts the swing thesis it is long on")

    return True, ("one symbol, one book — enforced from both sides, with an "
                  "explicit same-direction carve-out for INTRADAY joining a "
                  "SWING holding")


#: Every site that must be direction-aware before a short may be taken, and the
#: marker that proves it is. Textual because these are the CONSUMERS of
#: `direction` — the failure being guarded against is a module that reads the
#: field and does long-only arithmetic with it, which no import check can see.
_SHORT_SPINE = [
    ("exit ladder",      "intraday/exit_policy.py",        "D.gain_r(entry, ltp, risk, d)"),
    ("stop comparison",  "intraday/exit_policy.py",        "D.is_better_price(ltp, sl, d)"),
    ("cover deadline",   "intraday/exit_policy.py",        "intraday_short_cover_lead_min"),
    # `ref`, not `ltp`, since 12-Aug-2026: the invalidation compares either the
    # last price or the last COMPLETED bar close, depending on
    # intraday_invalidation_require_close, and `ref` is whichever is in force.
    # The property this line guards is unchanged and is the only one that
    # matters — the comparison is still made THROUGH D, so it is still
    # direction-aware. This check caught the rename the moment it landed, which
    # is the check working, not the check being in the way.
    ("invalidation",     "intraday/exit_policy.py",        "D.is_better_price(ref, breached, d)"),
    ("cost model",       "intraday/cost_model.py",         "D.reward_per_share(entry_price, target_price, direction)"),
    ("allocator scorer", "allocation/scoring.py",          "D.validate(entry, stop, target, direction)"),
    # 18-Aug-2026: the keying became a LIST comprehension when priors gained a
    # per-engine key alongside the family one (`keys = [f"{k}/SHORT" ...]`), so
    # the old `f"{key}/SHORT"` literal stopped matching while the behaviour was
    # unchanged. A marker that tracks a variable name rather than the property
    # it stands for fails on a rename; this one is still a grep, so it is still
    # weak — `tests/test_allocator_direction.py` and `test_engine_own_prior.py`
    # are what actually prove a short resolves to INTRADAY/SDN/SHORT.
    ("priors",           "allocation/scoring.py",          '/SHORT" for k in keys'),
    ("outcome resolver", "intraday/outcomes.py",           "hi >= stop, lo <= tgt"),
    # `dirn`, not `d`, since 15-Aug-2026 — and the rename is the POINT, not
    # cosmetic. `d` was already bound to the TRADE DATE at the top of
    # resolve_day, so `d = D.normalise(...)` inside the row loop destroyed it,
    # and the success log (`outcomes {d}:`) printed the last setup's direction
    # instead of the session it had just scored. The direction arithmetic was
    # always correct; the line that tells you WHICH DAY was scored was not, and
    # that is the line you read when asking why a day never was. This check
    # caught the rename the moment it landed — the check working, not the check
    # being in the way.
    ("outcome sign",     "intraday/outcomes.py",           "D.gain_pct(entry, exit_px, dirn)"),
    ("setup levels",     "intraday/strategies/base.py",    "D.risk_per_share(self.entry, self.stop, self.direction)"),
    # ── not yet built. The switch must stay off until these land. ──────────
    ("market context",   "intraday/market_context.py",     "allow_shorts"),
    ("structure gate",   "analysis/market_structure.py",   "def gate_short"),
    ("excursion",        "intraday/engine.py",             "D.favourable_excursion"),
    ("entry side",       "intraday/engine.py",             "D.entry_side"),
    ("cover on squareoff", "intraday/engine.py",           "D.exit_side"),
    ("shortability",     "intraday/shortability.py",       "def can_short"),
    # FOUND DURING MERGE REVIEW, migration 047. Every site above is a
    # CONSUMER of direction — it reads pos.get("direction") from an
    # open_positions row. None of them is the site that WRITES one, and
    # nothing here checks that write exists, which is exactly how it was
    # missing: every marker above was present, check_shorts reported the
    # spine complete, and a short would still have opened with no direction
    # recorded — read back as LONG by every one of these correctly-converted
    # sites. Text presence and a value's ability to round-trip through the
    # database are different claims; see the schema check below the marker
    # loop, which is the one assertion in this function that is not a grep.
    ("position write",   "execution/paper_broker.py",      '"direction": setup.get("direction")'),
    ("position write (caller)", "intraday/engine.py",       '"direction": st.direction'),
    # FOUND DURING A PRE-SESSION READINESS SWEEP, migration 049 — the same
    # shape of gap as the merge review above, three call sites over. Every
    # entry above proves the CALLEE is direction-aware. None of them proves
    # a caller actually PASSES the argument, and the default is LONG, so an
    # omitted argument is not a crash — it is a short silently scored,
    # structure-gated, or cost-checked as if it were a long. Found by testing
    # is_worth_taking() with the literal call from evaluate_intraday_setups()
    # and getting "target is on the wrong side of entry for a LONG" on a
    # coherent short.
    ("cost gate (caller)", "intraday/engine.py",
     "ok, why = is_worth_taking(best.entry, qty, best.target, best.stop,\n"
     "                                      direction=best.direction)"),
    ("allocator prior carried", "allocation/proposal.py",    'direction   = getattr(setup, "direction"'),
    ("allocator coherence",     "allocation/proposal.py",    "from intraday.direction import validate"),
    # 11-Aug-2026: marker updated from "direction=p.direction)" to
    # "direction=p.direction," -- the S.score() call at this site grew two
    # new trailing kwargs (engine_family, market_state; see
    # allocation/scoring.py::regime_fit_multiplier(), shipped at weight 0.0)
    # so direction=p.direction is no longer the LAST argument. The claim
    # this marker verifies (direction is passed through) is unchanged; only
    # what immediately follows it in the source changed.
    ("allocator scorer (caller)", "allocation/allocator.py", "direction=p.direction,"),
    ("allocator prior ladder",  "allocation/allocator.py",   '/SHORT")'),
    ("simulate cost gate",      "tools/simulate.py",
     "is_worth_taking(best.entry, qty, best.target, best.stop,\n"
     "                                      direction=best.direction) if qty"),
    ("simulate structure gate", "tools/simulate.py",
     'gate_for_framework(\n            "INTRADAY", [b.high for b in ctx.bars], [b.low for b in ctx.bars],\n'
     "            direction=best.direction)"),
]


def check_shorts() -> tuple[bool, str]:
    """
    Is short selling coherent end-to-end, or merely switched on?

    SHORTING IS A SIGN CONVENTION, NOT AN ENGINE. `direction` has been a field on
    Setup and a column on intraday_setups since migration 014; every engine
    hardcoded "LONG" and nothing read it. Eleven modules were therefore written
    assuming the field could not vary, and each fails differently and silently:

      · exit_policy computed risk = entry - stop, NEGATIVE for a short, fell
        back to a 0.5% default and then reported a losing short as a winner —
        a 2% adverse move read as +4.00R, which books a partial and trails the
        stop the wrong way
      · outcomes.resolve_day tested `lo <= stop`, true on the FIRST BAR when the
        stop sits above entry, so every short resolved as an instant loss and
        taught the learning loop that the engine was catastrophic
      · scoring.score treated `stop >= entry` as incoherent, so the allocator
        DECLINED every short before any engine's opinion was consulted
      · open_positions had no direction column and nothing wrote one, so a
        short that opened correctly would be MANAGED as a long for its entire
        life — found during merge review, after every marker-based check
        below already reported the spine complete, which is why this
        function no longer trusts markers alone (see the schema check)

    So the switch is not the capability. This check refuses to call shorting
    healthy until every consumer of `direction` has been made to read it, and it
    FAILS — rather than warns — if the switch is on while any site is missing.
    A partially-converted spine is worse than no shorts at all, because the
    long-only sites produce confident wrong numbers rather than errors.
    """
    from pathlib import Path
    from config import cfg_bool, get_supabase
    root = Path(__file__).parent.parent

    done, missing = [], []
    for label, rel, marker in _SHORT_SPINE:
        f = root / rel
        try:
            src = f.read_text(encoding="utf-8")
        except OSError:
            missing.append(label)
            continue
        (done if marker in src else missing).append(label)

    # THE ONE ASSERTION HERE THAT IS NOT A GREP.
    #
    # Every entry above proves a file CONTAINS the right words. None of them
    # proves a short position, once opened, can be told apart from a long one
    # by anything that reads it back — which is exactly the gap that shipped:
    # every marker present, the spine reported complete, and open_positions
    # had no column to hold the value the whole spine is built to interpret.
    # A live schema probe is the only check in this function a passing grep
    # cannot satisfy on its own.
    try:
        get_supabase().table("open_positions").select("direction").limit(1).execute()
        done.append("open_positions.direction column")
    except Exception as e:
        missing.append(f"open_positions.direction column ({str(e)[:60]})")

    # THE SECOND ASSERTION THAT IS NOT A GREP: can the runway gate be CLEARED?
    #
    # A complete, direction-aware spine still takes zero shorts if the gate in
    # front of it is unclearable. engine.py asked for
    # `getattr(st, "minutes_to_close", 0)`; SessionState has no such field, so
    # the default 0 was taken on every call and every short saw a runway of
    # 0 - 10 = -10 minutes. -10 >= 75 is false at every hour of every session,
    # so no short ever cleared it — and the log read like a market with no
    # shortable names rather than like a broken gate. 523 refusals in one
    # 46-minute stretch on 2026-08-06, all reporting the identical -10.
    #
    # Grepping cannot see this: every marker above was present and correct.
    # So probe the actual number at a realistic mid-morning instant, and assert
    # BOTH directions — a gate that always passes is as broken as one that
    # never does, just less visibly.
    try:
        from datetime import datetime as _dt
        from config import IST as _IST, cfg_int as _ci
        from intraday.session import minutes_to_cover_deadline as _mtcd
        need = _ci("intraday_short_min_runway_min", 75)
        base = _dt.now(_IST)
        early = _mtcd(base.replace(hour=10, minute=0, second=0, microsecond=0))
        late  = _mtcd(base.replace(hour=15, minute=10, second=0, microsecond=0))
        if early < need:
            missing.append(
                f"short runway gate is UNCLEARABLE — at 10:00 it reports "
                f"{early} min against a {need} min minimum, so no short can "
                f"ever be taken")
        elif late >= need:
            missing.append(
                f"short runway gate never CLOSES — at 15:10 it still reports "
                f"{late} min against a {need} min minimum, so a short could be "
                f"opened with no time to cover")
        else:
            done.append(f"short runway clearable (10:00 → {early} min, "
                        f"15:10 → {late} min, needs {need})")
    except Exception as e:
        missing.append(f"short runway gate not probeable ({str(e)[:60]})")

    # +2 for the two probes above that are not text markers — the schema probe
    # and the runway probe — neither counted by len(_SHORT_SPINE). Without this
    # a message could say "N/N complete" in the same breath as naming something
    # still missing, which is the exact contradiction a marker-only check would
    # print.
    total = len(_SHORT_SPINE) + 2
    on = cfg_bool("intraday_allow_shorts", False)
    if on and missing:
        return False, (
            f"intraday_allow_shorts is ON but {len(missing)} of "
            f"{total} direction-aware sites are missing: "
            f"{', '.join(missing[:6])}"
            + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else "")
            + ". A half-converted spine does not refuse shorts, it MIS-PRICES "
              "them — the long-only sites return confident wrong numbers rather "
              "than errors. Turn the switch off until these land.")
    if missing:
        return True, (f"shorts OFF — spine {len(done)}/{total} complete, "
                      f"still to build: {', '.join(missing[:4])}"
                      + (f" (+{len(missing) - 4} more)" if len(missing) > 4 else ""))
    return True, (f"shorts {'ON' if on else 'off'} — all {total} direction-aware "
                  f"sites present, exit ladder and outcome resolver included")



def check_stops_holding() -> tuple[bool, str]:
    """
    Is any OPEN position already past the stop it was given?

    THE GAP THIS CLOSES — 10-Aug-2026. The operator saw GABRIEL sitting at
    -5.47% from entry and still held, and could not tell from the outside
    whether that was an ordinary drawdown inside a wide stop or a stop that
    should have fired and did not. Nothing in this file distinguished those
    two cases, and they demand opposite responses: the first is the system
    working, the second is real money bleeding through a broken exit.

    `check_broker_consistency` asks whether a GTT EXISTS for each position. It
    does not ask whether price has already gone through it. A resting order at
    the right price and an exit that actually fired are different claims, and
    the failure mode in between them — order accepted, never triggered, price
    kept going — is exactly the shape this project has hit before with a
    broker-side rejection that every readiness check passed.

    Judged against `active_sl` where present (it moves with trails) and
    `planned_stop` otherwise. A position with NO stop recorded fails too: an
    exit rule with no level to compare against cannot fire at all.
    """
    from config import get_supabase
    sb = get_supabase()
    rows = (sb.table("open_positions")
            .select("symbol,framework,direction,entry_price,current_price,"
                    "planned_stop,active_sl,status")
            .execute().data) or []
    live = [r for r in rows if (r.get("status") or "").upper() == "ACTIVE"]
    if not live:
        return True, "no open positions to check"

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    breached, nostop = [], []
    for r in live:
        entry, now = _f(r.get("entry_price")), _f(r.get("current_price"))
        stop = _f(r.get("active_sl")) or _f(r.get("planned_stop"))
        if not entry or not now:
            continue
        if not stop:
            nostop.append(r.get("symbol") or "?")
            continue
        is_short = (r.get("direction") or "LONG").upper() == "SHORT"
        through = (now > stop) if is_short else (now < stop)
        if through:
            pct = ((entry - now) if is_short else (now - entry)) / entry * 100.0
            breached.append(f"{r.get('symbol')} {pct:+.2f}% (stop {stop:g}, now {now:g})")

    if breached or nostop:
        parts = []
        if breached:
            parts.append("PAST ITS STOP AND STILL OPEN: " + ", ".join(breached[:6]))
        if nostop:
            parts.append("NO STOP RECORDED: " + ", ".join(nostop[:6]))
        return False, ("; ".join(parts)
                       + ". A stop that does not fire is an absent stop, not a wide "
                         "one. Check the GTT rests at that price and that "
                         "evaluate_exit is reached for this book "
                         "(tools.exit_audit --open-only shows every position).")
    return True, f"all {len(live)} open position(s) are on the correct side of their stop"


def check_quantity_fields() -> tuple[bool, str]:
    """
    Do current_qty, actual_qty and kite_qty agree, for every open position?

    THE GAP THIS CLOSES — 11-Aug-2026. PPLPHARMA booked 5 of 11 shares on
    10-Aug. The write that recorded it touched current_qty alone (6,
    correctly) and left actual_qty/kite_qty at the pre-partial 11 — and
    they STAYED there, confirmed against the live broker holding a day
    later (6 real shares, 11 in both mirror columns). The reason it never
    self-corrected: reconcile's own "already matches" fast path
    (position_lifecycle.py) compared ONLY current_qty against the broker,
    found agreement (current_qty was already right), and declared
    reconcile_status=MATCHED without ever looking at the other two fields.
    The dashboard's mismatch banner is gated on reconcile_status !=
    'MATCHED', so a false MATCHED did not just miss the drift — it
    actively hid it, on the exact row an operator would otherwise have
    seen it on.

    Both write paths are now fixed to keep all three fields together
    (engine.py's BOOK_PARTIAL, both PAPER and LIVE; position_lifecycle.py's
    reconcile fast path as a second line of defence). This check is the
    THIRD line: it does not trust either fix to keep working — it directly
    asks the only question that matters, on every open position, every
    time health runs. A future code path that repeats the same mistake
    (updates current_qty without its mirrors) is caught here regardless of
    which file it lives in.
    """
    from config import get_supabase
    sb = get_supabase()
    rows = (sb.table("open_positions")
            .select("symbol,framework,mode,status,current_qty,actual_qty,"
                    "kite_qty,reconcile_status")
            .eq("status", "ACTIVE").execute().data) or []
    if not rows:
        return True, "no open positions to check"

    def _i(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    drifted = []
    for r in rows:
        cur, act, kite = _i(r.get("current_qty")), _i(r.get("actual_qty")), _i(r.get("kite_qty"))
        vals = [v for v in (cur, act, kite) if v is not None]
        if len(vals) >= 2 and len(set(vals)) > 1:
            drifted.append(
                f"{r.get('symbol')} [{r.get('framework')}]: current_qty={cur} "
                f"actual_qty={act} kite_qty={kite} "
                f"(reconcile_status={r.get('reconcile_status')})")

    if drifted:
        shown = "; ".join(drifted[:5])
        if len(drifted) > 5:
            shown += f"; +{len(drifted) - 5} more"
        return False, (shown + ". These three columns must always agree — a "
                       "partial book or a reconcile pass that updates one "
                       "without the others leaves the dashboard's own "
                       "mismatch banner blind to real drift (it is gated on "
                       "reconcile_status, which a stale mirror field can "
                       "report as MATCHED). Run control.position_lifecycle's "
                       "reconcile to resync from the broker.")
    return True, f"current_qty/actual_qty/kite_qty agree on all {len(rows)} open position(s)"


CHECKS = [
    ("config",   "risk numbers contradict each other, or a switch does nothing", check_config,   False),
    ("shorts",   "a short is taken while some module still does long-only arithmetic", check_shorts, False),
    ("governance", "a parameter can change itself, or an unmeasured layer ranks trades", check_governance, False),
    ("allocator", "the allocator can reach an order path despite its switches", check_allocator_isolation, False),
    ("hurdle",   "the allocator's bar can never be cleared, so the book goes quiet", check_allocator_hurdle, False),
    ("books",    "one symbol ends up in both frameworks with contradictory exits", check_framework_isolation, False),
    ("storage",  "the database stops accepting writes and the pipeline goes silent", check_storage, False),
    ("feed",     "decisions run on data of unknown age, or ticks arrive late",  check_feed_integrity, False),
    ("quote_parity", "a live quote-mode field drifted from the historical endpoint and nobody is watching", check_quote_parity, False),
    ("exits",    "an exit rule can sell without alerting, or fires from only one caller", check_exit_actions, False),
    ("costs",    "charges are priced off a stale or wrong-product rate",         check_cost_rates, False),
    ("selects",  "a query reads a column the schema no longer has",              check_selects,  False),
    ("sort_keys", "a paged read sorts on a column the table does not have, so it returns nothing", check_sort_keys, False),
    ("kite",     "no broker session, or the IP is not allowlisted",              check_kite,     False),
    ("data",     "decisions would run on stale inputs",                          check_data_freshness, False),
    ("broker",   "resting orders do not match the positions they protect",       check_broker_consistency, False),
    ("capital",  "TOTAL_CAPITAL drifts from what the broker account actually holds", check_capital, False),
    ("stops",    "an open position is trading past the stop that should have closed it", check_stops_holding, False),
    ("qty_fields", "current_qty/actual_qty/kite_qty silently disagree, hiding drift from the dashboard", check_quantity_fields, False),
    ("daemon",   "nothing is watching your positions right now",                 check_daemon,   False),
    ("pending",  "an entry order that never filled is being tracked as a real position", check_pending_fills, False),
    ("pending_dup", "a SWING symbol was bought twice within minutes — the F-67 shape", check_pending_fill_duplicates, False),
    ("pending_scale_in", "a Stage E7 add-on order never filled and nobody would otherwise notice", check_pending_scale_ins, False),
    ("sector_risk", "a majority of the open SWING book sits in sectors rotating away from it", check_sector_concentration_risk, False),
    ("recon_drift", "a closed position's entry_price/qty may not reflect reality — same-day settlement-lag shape, HINDCOPPER F-84/F-85", check_same_day_reconcile_drift, False),
    ("exits_open", "a SELL the system decided on is still unfilled and the position is unprotected", check_open_exits, False),
    ("learning", "engines are being judged on evidence that was never collected", check_learning_loop, False),
    ("simulate", "a stage completes while producing nothing",                    check_simulate, True),
]


def main(quick: bool = False) -> int:
    logger.info("═" * 72)
    logger.info(f"TradeOS health{'  (quick)' if quick else ''}")
    logger.info("═" * 72)

    results: list[Check] = []
    for name, what, fn, slow in CHECKS:
        c = Check(name=name, what=what)
        if quick and slow:
            c.skipped = True
            results.append(c)
            logger.info(f"  …  {name:<9} skipped (--quick)")
            continue
        c.ok, c.detail = _run(fn)
        results.append(c)
        if c.ok:
            logger.success(f"  ✓  {name:<9} {c.detail}")
        else:
            logger.error(f"  ✗  {name:<9} {c.detail.splitlines()[0][:120]}")
            logger.info(f"       means: {what}")

    bad = [c for c in results if not c.ok and not c.skipped]
    logger.info("")
    logger.info("─" * 72)
    if bad:
        logger.error(f"  {len(bad)} PROBLEM(S): {', '.join(c.name for c in bad)}")
        logger.info("")
        logger.info("  Fix these before trusting today's decisions.")
    else:
        n = len([c for c in results if not c.skipped])
        logger.success(f"  all {n} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run every TradeOS health check")
    ap.add_argument("--quick", action="store_true",
                    help="skip slow checks — used by the launcher")
    sys.exit(main(ap.parse_args().quick))
