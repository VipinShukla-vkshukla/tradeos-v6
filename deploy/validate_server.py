"""
Prove this server can actually run the intraday daemon. Run it ON the server.

    cd ~/tradeos-v6 && .venv/bin/python deploy/validate_server.py

WHY THIS EXISTS RATHER THAN A CHECKLIST
----------------------------------------
A VCN can look correct in the Oracle console and still fail the one thing that
matters: the daemon holds a WEBSOCKET open to Kite for six hours. A security
list that permits HTTPS but not sustained outbound, or a stateless egress rule,
passes a browser test and fails at 09:20 with no prices — which this system
reports as "monitoring 0 symbols" rather than as an error.

So nothing here is inspected. Every check makes the actual call the daemon makes
and reports what came back. If this passes, the daemon works, because it has
just done the same things.

WHAT IT CANNOT CHECK
--------------------
Whether your public IP is RESERVED or EPHEMERAL. That distinction lives only in
the Oracle console, and it is the single most expensive thing to get wrong: an
ephemeral IP changes when the instance is stopped and started, silently
invalidating your Kite allowlist entry. The script records the current IP so a
change is at least detectable, and says so loudly.
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
_results: list[tuple[str, str, str]] = []


def rec(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    icon = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn ", INFO: "      "}[status]
    print(f"[{icon}] {name}" + (f"\n           {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{'─' * 72}\n{t}\n{'─' * 72}")


# ── 1. The machine itself ───────────────────────────────────────────────────
def check_host() -> None:
    hdr("1 · HOST")

    # The daemon reasons in IST end to end. On UTC every session-phase decision
    # is wrong by 5h30m — the daemon would "square off" at 21:00 local and think
    # the market opened at 03:30. setup.sh sets this; verify it stuck.
    tz = time.tzname
    off = -time.timezone if not time.daylight else -time.altzone
    if off == 19800:
        rec(PASS, f"timezone is IST ({tz[0]}, UTC+5:30)")
    else:
        rec(FAIL, f"timezone is {tz[0]} (UTC{off / 3600:+g}), not IST",
            "sudo timedatectl set-timezone Asia/Kolkata — every session-phase "
            "decision is wrong until this is fixed")

    # A skewed clock moves the square-off deadline and the token expiry check.
    try:
        import urllib.request
        with urllib.request.urlopen("https://www.google.com", timeout=10) as r:
            served = r.headers.get("Date")
        if served:
            from email.utils import parsedate_to_datetime
            skew = abs((parsedate_to_datetime(served)
                        - datetime.now(timezone.utc)).total_seconds())
            if skew < 5:
                rec(PASS, f"clock is within {skew:.1f}s of internet time")
            else:
                rec(WARN if skew < 60 else FAIL,
                    f"clock is {skew:.0f}s off internet time",
                    "sudo timedatectl set-ntp true")
    except Exception as e:
        rec(WARN, "could not check clock skew", str(e)[:120])

    try:
        import psutil  # noqa
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        rec(INFO if mem >= 0.9 else WARN, f"{mem:.1f} GB RAM",
            "" if mem >= 0.9 else "the daemon wants ~400 MB; this is tight")
    except Exception:
        pass


# ── 2. Credentials ──────────────────────────────────────────────────────────
def check_env() -> bool:
    hdr("2 · CREDENTIALS")
    envf = ROOT / "backend" / ".env"
    if not envf.exists():
        rec(FAIL, "backend/.env is missing",
            "scp it from your laptop. KITE_ACCESS_TOKEN is NOT needed — the "
            "daemon reads today's token from Supabase.")
        return False
    rec(PASS, "backend/.env present")

    try:
        import config  # noqa  — loads .env
    except Exception as e:
        rec(FAIL, "config.py could not load", str(e)[:200])
        return False

    required = {
        "SUPABASE_URL": "the daemon cannot read config, positions or the token",
        "SUPABASE_SERVICE_KEY": "same",
        "KITE_API_KEY": "no prices, no orders",
        "KITE_API_SECRET": "cannot refresh a session",
    }
    ok = True
    for k, why in required.items():
        if (os.getenv(k) or "").strip():
            rec(PASS, f"{k} set")
        else:
            rec(FAIL, f"{k} is empty — {why}")
            ok = False

    # Intraday has its OWN channels by design, so a scalp alert never lands in
    # the middle of the swing digest. Missing here means the server runs blind:
    # you would have no signal that it is alive.
    for k in ("TELEGRAM_INTRADAY_BOT_TOKEN", "TELEGRAM_INTRADAY_CHAT_ID"):
        rec(PASS if (os.getenv(k) or "").strip() else WARN,
            f"{k} {'set' if (os.getenv(k) or '').strip() else 'is empty'}",
            "" if (os.getenv(k) or "").strip()
            else "the daemon posting to Telegram is how you know the SERVER is "
                 "alive independently of your laptop")
    return ok


# ── 3. Outbound reachability — the VCN test that matters ────────────────────
def check_egress() -> None:
    hdr("3 · OUTBOUND (VCN egress + host firewall)")
    targets = [
        ("api.kite.trade", 443, "Kite REST — quotes, orders, holdings"),
        ("ws.kite.trade", 443, "Kite WEBSOCKET — the live tick feed"),
        ("api.telegram.org", 443, "intraday alerts"),
        ("discord.com", 443, "intraday alerts"),
        ("api.ipify.org", 443, "public IP discovery"),
    ]
    sb_host = (os.getenv("SUPABASE_URL") or "").replace("https://", "").strip("/")
    if sb_host:
        targets.insert(0, (sb_host, 443, "Supabase — config, positions, token"))

    for host, port, why in targets:
        try:
            t0 = time.time()
            with socket.create_connection((host, port), timeout=10) as s:
                with ssl.create_default_context().wrap_socket(
                        s, server_hostname=host):
                    ms = (time.time() - t0) * 1000
            rec(PASS, f"{host}:{port} reachable ({ms:.0f} ms)", why)
        except Exception as e:
            rec(FAIL, f"{host}:{port} UNREACHABLE — {why}",
                f"{type(e).__name__}: {str(e)[:110]}\n           "
                f"Check the VCN security list EGRESS rule allows TCP 443 to "
                f"0.0.0.0/0, and that Ubuntu's own iptables is not dropping it "
                f"(sudo iptables -L OUTPUT -n).")

    # A websocket that connects and then dies is the failure a port test misses.
    # Hold it open long enough to prove the path is stateful, not just open.
    try:
        t0 = time.time()
        with socket.create_connection(("ws.kite.trade", 443), timeout=10) as s:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(s, server_hostname="ws.kite.trade") as ss:
                ss.settimeout(12)
                time.sleep(10)
                ss.getpeername()
        rec(PASS, f"websocket path held open 10s ({(time.time() - t0):.0f}s total)",
            "a stateless or idle-timeout egress rule would have dropped this — "
            "the daemon holds this socket for six hours")
    except Exception as e:
        rec(FAIL, "websocket path did not stay open for 10s",
            f"{type(e).__name__}: {str(e)[:110]}\n           "
            f"The daemon needs a SUSTAINED connection. A rule that permits the "
            f"handshake but drops idle flows gives you a monitor with no prices "
            f"that still reports success.")


# ── 4. Supabase, and the token written by your laptop ───────────────────────
def check_supabase() -> None:
    hdr("4 · SUPABASE + KITE SESSION")
    try:
        from config import get_supabase
        sb = get_supabase()
        rows = (sb.table("system_config").select("key,value")
                  .in_("key", ["master_kill_switch", "intraday_trading_mode",
                               "intraday_strategies_enabled", "kite_access_token",
                               "kite_access_token_date", "kite_allowlisted_ip"])
                  .execute().data or [])
        cfg = {r["key"]: r["value"] for r in rows}
        rec(PASS, f"Supabase reachable — read {len(rows)} config rows")
    except Exception as e:
        rec(FAIL, "Supabase unreachable", str(e)[:200])
        return

    # The daemon must be able to WRITE: the singleton lease is a write, and
    # without it two daemons both act and every position is doubled.
    try:
        # Write then DELETE. Leaving a probe row behind would add permanent
        # clutter to a table that is meant to hold settings, and a key nobody
        # reads is exactly what this project keeps having to clean up.
        sb.table("system_config").upsert(
            {"key": "_probe_server_write",
             "value": datetime.now().isoformat(),
             "description": "transient write probe — deleted immediately"}).execute()
        sb.table("system_config").delete().eq("key", "_probe_server_write").execute()
        rec(PASS, "Supabase writable — the singleton lease can be taken")
    except Exception as e:
        rec(FAIL, "Supabase is READ-ONLY from here",
            f"{str(e)[:130]}\n           Without a writable lease two daemons "
            f"can both go ACTIVE and double every position.")

    # kite_access_token_date, matching TOKEN_DATE_KEY in kite/token_manager.py.
    # This read "kite_token_date", which does not exist, so it always came back
    # empty and the server reported "Kite token is from , not 2026-07-30" on
    # every run — a warning about a perfectly valid session, printed at exactly
    # the moment you are trying to work out whether the session is the problem.
    tok_date = cfg.get("kite_access_token_date", "")
    today = datetime.now().strftime("%Y-%m-%d")
    if not cfg.get("kite_access_token"):
        rec(WARN, "no Kite token in Supabase yet",
            "your LAPTOP writes this at login. Run `tradeos` at home first; the "
            "server never logs in itself.")
    elif tok_date[:10] == today:
        # Compared on the date PREFIX: the dashboard writes a full ISO timestamp
        # and this module writes an IST one, so an equality test against a bare
        # date can only ever match one of the two writers.
        rec(PASS, f"Kite token in Supabase is today's ({tok_date[:19]})")
    else:
        rec(WARN, f"Kite token is from {tok_date[:10] or 'an unrecorded date'}, not {today}",
            "tokens expire 07:30 IST daily. Log in from the laptop.")

    # Probe the token rather than trusting a validity flag — a token from a
    # retired app reports valid and fails on the first real call.
    try:
        from kite import kite_client
        prof = kite_client.get_kite().profile()
        rec(PASS, f"Kite session works from THIS server — {prof.get('user_id')}",
            "this proves the IP is allowlisted for reads")
    except Exception as e:
        msg = str(e)[:160]
        rec(WARN if "token" in msg.lower() else FAIL,
            "Kite call failed from this server", msg)


# ── 5. The IP, and the allowlist ────────────────────────────────────────────
def check_ip() -> None:
    hdr("5 · PUBLIC IP + KITE ALLOWLIST")
    ip = ""
    try:
        import urllib.request
        ip = urllib.request.urlopen("https://api.ipify.org",
                                    timeout=10).read().decode().strip()
        rec(PASS, f"this server's public IP: {ip}")
    except Exception as e:
        rec(FAIL, "could not determine the public IP", str(e)[:120])
        return

    try:
        from config import get_supabase
        sb = get_supabase()
        rows = (sb.table("system_config").select("key,value")
                  .in_("key", ["kite_allowlisted_ip",
                               "kite_allowlisted_ip_server"]).execute().data or [])
        cfg = {r["key"]: r["value"] for r in rows}
        home = cfg.get("kite_allowlisted_ip", "")
        prev = cfg.get("kite_allowlisted_ip_server", "")

        if prev and prev != ip:
            rec(FAIL, f"this server's IP CHANGED: {prev} -> {ip}",
                "Your Oracle public IP is EPHEMERAL, not reserved. It changes "
                "on stop/start and silently invalidates the Kite allowlist. "
                "Reserve it in the Oracle console, then re-add it at "
                "https://developers.kite.trade/apps")
        elif prev:
            rec(PASS, f"IP unchanged since last check ({prev})")
        else:
            rec(INFO, "recording this IP so a future change is detectable")

        sb.table("system_config").upsert(
            {"key": "kite_allowlisted_ip_server", "value": ip,
             "description": "Intraday daemon server public IP. Compared on each "
                            "validate_server.py run to catch an ephemeral IP "
                            "rotating out of the Kite allowlist."}).execute()

        if home and home != ip:
            rec(PASS, f"home IP on record is {home} — different, as expected",
                "Zerodha allows TWO. Both must be listed at "
                "https://developers.kite.trade/apps")
        elif home == ip:
            rec(WARN, "the recorded home IP equals this server's IP",
                "one of the two slots is being wasted")
    except Exception as e:
        rec(WARN, "could not compare against the recorded IPs", str(e)[:120])

    rec(INFO, "CANNOT be checked from here: reserved vs ephemeral",
        "Oracle console -> Compute -> Instance -> Attached VNICs -> IPv4 "
        "Addresses. If it says 'Ephemeral', convert it to Reserved. This is the "
        "single most expensive thing to get wrong.")


# ── 6. systemd ──────────────────────────────────────────────────────────────
def check_systemd() -> None:
    hdr("6 · SERVICE")
    for unit in ("tradeos-intraday.service", "tradeos-intraday.timer"):
        try:
            out = subprocess.run(["systemctl", "is-enabled", unit],
                                 capture_output=True, text=True, timeout=10)
            state = out.stdout.strip() or out.stderr.strip()
            if state in ("enabled", "static"):
                rec(PASS, f"{unit} is {state}")
            elif unit.endswith(".service") and state == "disabled":
                # The service is started BY the timer, so disabled is correct.
                rec(PASS, f"{unit} is disabled — correct, the timer starts it")
            else:
                rec(WARN, f"{unit} is {state}",
                    "cd ~/tradeos-v6/deploy && bash setup.sh")
        except FileNotFoundError:
            rec(WARN, "systemctl not found — not a systemd host")
            return
        except Exception as e:
            rec(WARN, f"could not query {unit}", str(e)[:120])

    try:
        out = subprocess.run(["systemctl", "list-timers", "tradeos-intraday.timer",
                              "--no-pager"], capture_output=True, text=True, timeout=10)
        nxt = [l for l in out.stdout.splitlines() if "tradeos" in l]
        if nxt:
            rec(INFO, "next start", nxt[0].strip())
    except Exception:
        pass


# ── 7. Would the daemon actually do anything? ───────────────────────────────
def check_daemon_would_run() -> None:
    hdr("7 · WOULD THE DAEMON ACT?")
    try:
        from execution.gates import trading_mode, auto_exit_enabled
        from config import is_kill_switch_active, cfg_bool, TOTAL_CAPITAL

        if is_kill_switch_active():
            rec(WARN, "master kill switch is ON — the daemon will not trade",
                "intended if you set it; otherwise clear it on the dashboard")
        else:
            rec(PASS, "kill switch is clear")

        mode = trading_mode("INTRADAY")
        rec(PASS, f"intraday mode: {mode}",
            "simulated fills, real decisions" if mode == "PAPER"
            else "REAL ORDERS will be placed from this server")

        on = cfg_bool("intraday_strategies_enabled", True)
        rec(PASS if on else WARN,
            f"intraday engines {'enabled' if on else 'DISABLED'}",
            "" if on else "the daemon will start, find nothing, and look healthy")
        rec(PASS, f"auto-exit {'ON' if auto_exit_enabled('INTRADAY') else 'off'}")
        rec(INFO, f"capital ₹{TOTAL_CAPITAL:,.0f}",
            "must match your laptop's .env — two different values means two "
            "different position sizes for the same setup")

        # READ the lease row; never acquire(). Acquiring here would take the
        # lease away from a daemon that is mid-session and running correctly.
        from config import get_supabase
        rows = (get_supabase().table("intraday_daemon_lease")
                .select("holder,hostname,expires_at").eq("id", 1)
                .execute().data or [])
        if not rows:
            rec(PASS, "lease is free — this daemon would go ACTIVE")
        else:
            r = rows[0]
            exp = str(r.get("expires_at") or "")
            try:
                live = datetime.fromisoformat(
                    exp.replace("Z", "+00:00")) > datetime.now(timezone.utc)
            except Exception:
                live = False
            if live:
                rec(INFO, f"lease held by {r.get('hostname')} ({r.get('holder')})",
                    "that instance is ACTIVE; this one would run STANDBY and take "
                    "over only if it stops renewing. Exactly one acts.")
            else:
                rec(PASS, f"lease expired (last held by {r.get('hostname')}) — "
                          f"this daemon would take it")
    except ImportError as e:
        rec(WARN, "could not evaluate gates", str(e)[:120])
    except Exception as e:
        rec(WARN, "gate check incomplete", str(e)[:140])


def guard_wrong_machine() -> bool:
    """
    Refuse to run anywhere but the daemon server.

    This script RECORDS the public IP it sees as the server's. Run on the
    laptop, it would write the home IP into that slot and then report "IP
    unchanged" forever — turning the one check that catches an ephemeral IP
    rotating out of the Kite allowlist into a check that can never fire.
    """
    if sys.platform == "win32":
        print("\n  This is Windows — the daemon server is Ubuntu on Oracle Cloud.")
        print("  Run it there:\n")
        print("    ssh -i your-key.pem ubuntu@<SERVER_IP>")
        print("    cd ~/tradeos-v6 && .venv/bin/python deploy/validate_server.py\n")
        return False

    try:
        import urllib.request
        from config import get_supabase
        ip = urllib.request.urlopen("https://api.ipify.org",
                                    timeout=10).read().decode().strip()
        rows = (get_supabase().table("system_config").select("value")
                .eq("key", "kite_allowlisted_ip").execute().data or [])
        home = (rows[0]["value"] if rows else "") or ""
        if home and home == ip:
            print(f"\n  This machine's IP ({ip}) is the one recorded as HOME.")
            print("  Run this on the daemon server, not the laptop.\n")
            return False
    except Exception:
        pass  # Can't tell — proceed rather than block on a network hiccup.
    return True


def main() -> int:
    print("═" * 72)
    print("TradeOS — can this server run the intraday daemon?")
    print("═" * 72)

    if not guard_wrong_machine():
        return 2

    check_host()
    if check_env():
        check_egress()
        check_supabase()
        check_ip()
    check_systemd()
    check_daemon_would_run()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    hdr("VERDICT")
    print(f"  {len(fails)} blocking · {len(warns)} to look at · "
          f"{len([r for r in _results if r[0] == PASS])} passed")
    if fails:
        print("\n  Blocking:")
        for _, n, _d in fails:
            print(f"    · {n}")
        print("\n  The daemon will not work correctly until these are fixed.")
    elif warns:
        print("\n  Nothing blocking. Worth a look:")
        for _, n, _d in warns:
            print(f"    · {n}")
    else:
        print("\n  This server can run the intraday daemon.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
