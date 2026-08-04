"""
TradeOS v7 — Kite Access Token Manager
=======================================
Zerodha access tokens expire every morning at ~07:30 IST. There is no
refresh-token flow: obtaining a new one always requires an interactive login.

WHY THE TOKEN LIVES IN system_config
------------------------------------
The pipeline runs on GitHub Actions runners, which are ephemeral — a token
written to .env or to the filesystem is gone on the next run. Storing it in
system_config means the daily login happens ONCE, on any machine, and every
subsequent runner picks it up.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not automate the Zerodha login. Doing so requires storing your account
password and TOTP seed and driving the 2FA form — that hands full trading
authority to an unattended script, and a leaked seed is equivalent to a leaked
account. The daily step here is a single tap on a link.

DAILY FLOW (about 15 seconds)
-----------------------------
  1. `python -m kite.token_manager --login-url` prints the Zerodha login URL.
     The morning Telegram brief also includes it whenever the token is stale.
  2. Open it, log in. Zerodha redirects to your registered redirect URL with
     `?request_token=XXXX` in the query string.
  3. `python -m kite.token_manager --exchange XXXX`
     Exchanges it for an access token and writes it to system_config.

Everything downstream (sl_monitor, position_lifecycle, execution_engine)
calls get_access_token() and degrades gracefully when it returns None.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import KITE_API_KEY, KITE_API_SECRET, IST, get_supabase

TOKEN_KEY      = "kite_access_token"
TOKEN_DATE_KEY = "kite_access_token_date"


def _parse_issued(raw: str):
    """
    Parse the stored token timestamp, whichever writer produced it.

    TWO WRITERS, TWO FORMATS, AND A PYTHON VERSION THAT CARES
    ---------------------------------------------------------
    The dashboard callback writes JavaScript's toISOString(), which ends in a
    literal 'Z'. This module writes datetime.isoformat(), which uses a +05:30
    offset. Both are valid ISO 8601.

    datetime.fromisoformat() only learned to accept 'Z' in Python 3.11. The
    laptop runs 3.12 and parsed it; the Oracle server runs Ubuntu 22.04 with
    Python 3.10 and did not. So a token written by the dashboard was read as
    "unparseable — treating as expired" on the server ONLY, and the daemon spent
    the session with no prices while the same token worked perfectly at home.

    Nothing in the error mentioned the version, the writer, or the 'Z'. It said
    the token was expired, which was false in the way that costs a session.

    Normalising here rather than at the writer because both writers are correct
    and the reader is the thing that has to cope with a value it did not write.
    """
    if not raw:
        return None
    s = str(raw).strip()
    # 'Z' is UTC. Python < 3.11 rejects it outright.
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    for attempt in (s, s.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(attempt)
            return IST.localize(dt) if dt.tzinfo is None else dt
        except ValueError:
            continue
    # A bare date is what the very first version of this stored.
    try:
        return IST.localize(datetime.strptime(s[:10], "%Y-%m-%d"))
    except ValueError:
        return None


# The api_key the stored token was minted under.
#
# Without this, swapping the Kite Connect app (new api_key in .env) leaves a
# token from the OLD app in system_config, and every freshness check reports
# valid=True because it only looks at the date. Every subsequent call then
# fails with "Incorrect `api_key` or `access_token`" — a TokenException that
# reads like an expiry problem but is actually an identity mismatch, and no
# amount of waiting fixes it. Recording the issuer lets us detect the swap and
# say so.
TOKEN_APIKEY_KEY = "kite_access_token_api_key"

# Zerodha invalidates tokens daily at ~07:30 IST. Treat anything issued before
# today's 07:30 boundary as expired rather than trusting a bare date match,
# so a token minted at 23:00 last night is correctly rejected at 09:00 today.
TOKEN_EXPIRY_HOUR   = 7
TOKEN_EXPIRY_MINUTE = 30


def _set_config(key: str, value: str) -> None:
    sb = get_supabase()
    existing = sb.table("system_config").select("key").eq("key", key).execute().data
    if existing:
        sb.table("system_config").update({"value": value}).eq("key", key).execute()
    else:
        sb.table("system_config").insert({
            "key": key, "value": value,
            "description": "Kite Connect session — written by kite.token_manager",
        }).execute()


def _get_config(key: str) -> str | None:
    try:
        rows = get_supabase().table("system_config").select("value").eq("key", key).execute().data
        return rows[0]["value"] if rows else None
    except Exception as e:
        logger.warning(f"  token_manager: could not read {key}: {e}")
        return None


def _last_expiry_boundary(now: datetime) -> datetime:
    """The most recent 07:30 IST boundary at or before `now`."""
    boundary = now.replace(hour=TOKEN_EXPIRY_HOUR, minute=TOKEN_EXPIRY_MINUTE,
                           second=0, microsecond=0)
    return boundary if now >= boundary else boundary - timedelta(days=1)


def get_login_url() -> str:
    if not KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY not set in .env")
    return f"https://kite.zerodha.com/connect/login?api_key={KITE_API_KEY}&v=3"


def _extract_request_token(raw: str) -> str:
    """
    Accept either a bare request_token or the whole redirect URL.

    After login Zerodha redirects to something like
        https://your-redirect/?action=login&status=success&request_token=AbC123...
    Selecting just the token out of that is fiddly, so pasting the entire URL
    works too.
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if "request_token=" in raw:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(raw).query)
        token = (qs.get("request_token") or [""])[0]
        if token:
            return token.strip()
    return raw


def exchange_request_token(request_token: str) -> str:
    """
    Swap a one-shot request_token for an access_token and persist it.
    The request_token is single-use and expires within minutes of the redirect.
    """
    if not KITE_API_KEY or not KITE_API_SECRET:
        raise RuntimeError("KITE_API_KEY and KITE_API_SECRET must both be set in .env")

    request_token = _extract_request_token(request_token)

    # Validate locally rather than letting Kite reject it. The common mistake is
    # pasting the literal placeholder from the instructions, which produces
    # "InputException: `request_token` should be minimum 10 characters" - an
    # error that says nothing about what to do next.
    if request_token.upper() in {"XXXX", "REQUEST_TOKEN", "<REQUEST_TOKEN>", "YOUR_TOKEN"}:
        raise SystemExit(
            f"\n  '{request_token}' is the placeholder from the instructions, not a real token.\n\n"
            f"  1. Open:  {get_login_url()}\n"
            f"  2. Log in. Your browser lands on your redirect URL, which contains\n"
            f"     ?request_token=... in the address bar.\n"
            f"  3. Paste the whole URL (or just the token):\n"
            f"     python -m kite.token_manager --exchange \"<paste here>\"\n"
        )
    if len(request_token) < 10:
        raise SystemExit(
            f"\n  request_token '{request_token}' is only {len(request_token)} characters; "
            f"Kite requires at least 10.\n"
            f"  Copy it from the redirect URL after logging in at:\n    {get_login_url()}\n"
            f"  You can paste the entire redirect URL - the token is extracted automatically.\n"
        )

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=KITE_API_KEY)
    try:
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    except Exception as e:
        msg = str(e)
        hint = ""
        if "token" in msg.lower() and ("invalid" in msg.lower() or "expired" in msg.lower()):
            # request_tokens are single-use and short-lived; reusing one that
            # already succeeded, or one from a browser tab left open, is the
            # usual cause.
            hint = ("\n  request_tokens are SINGLE-USE and expire within minutes. "
                    "Log in again for a fresh one:\n"
                    f"    {get_login_url()}")
        raise SystemExit(f"\n  Kite rejected the token: {msg}{hint}\n")
    access_token = data["access_token"]

    now = datetime.now(IST)
    _set_config(TOKEN_KEY, access_token)
    _set_config(TOKEN_DATE_KEY, now.isoformat())
    _set_config(TOKEN_APIKEY_KEY, KITE_API_KEY)
    logger.success(
        f"✓ Kite access token stored for user {data.get('user_id', '?')} "
        f"— valid until ~07:30 IST tomorrow"
    )
    return access_token


def get_access_token() -> str | None:
    """
    Current access token, or None when absent/expired.

    Never raises: every caller is expected to degrade gracefully rather than
    take the pipeline down because a broker session lapsed.
    """
    import os

    # Env var wins — lets a CI run inject a token without a DB round trip.
    env_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token

    token    = _get_config(TOKEN_KEY)
    issued_s = _get_config(TOKEN_DATE_KEY)
    if not token or not issued_s:
        logger.warning(
            "  Kite access token not set. Run: python -m kite.token_manager --login-url"
        )
        return None

    # Identity check before freshness. A token minted under a different app is
    # not stale, it is wrong — and re-running the login is the only fix, so
    # say that instead of letting the caller discover a TokenException.
    issuer = _get_config(TOKEN_APIKEY_KEY)
    if issuer and KITE_API_KEY and issuer != KITE_API_KEY:
        logger.warning(
            f"  Kite token was issued for a DIFFERENT app (api_key {issuer[:8]}…) "
            f"but .env now uses {KITE_API_KEY[:8]}…. Re-authenticate: "
            f"python -m kite.token_manager --login-url"
        )
        return None

    # No recorded issuer means the token predates that bookkeeping, so the
    # identity check above proves nothing and the date check will happily call
    # it valid. That is how a token from a retired app kept reporting valid=True
    # while every single API call returned TokenException. When we cannot prove
    # ownership from stored state, ask the broker once and cache the answer.
    if not issuer and token:
        if _probe_token(token):
            _set_config(TOKEN_APIKEY_KEY, KITE_API_KEY)   # proven; skip next time
        else:
            logger.warning(
                "  Stored Kite token is not accepted by the current app "
                f"({KITE_API_KEY[:8]}…) — it was minted before issuer tracking "
                "existed and belongs to a different Kite Connect app. "
                "Re-authenticate: python -m kite.token_manager --login-url"
            )
            return None

    issued = _parse_issued(issued_s)
    if issued is None:
        logger.warning(f"  Unparseable kite token date '{issued_s}' — treating as expired")
        return None

    now = datetime.now(IST)
    if issued < _last_expiry_boundary(now):
        age_h = (now - issued).total_seconds() / 3600
        logger.warning(
            f"  Kite access token EXPIRED (issued {issued:%Y-%m-%d %H:%M} IST, "
            f"{age_h:.1f}h ago, past the 07:30 boundary). "
            f"Run: python -m kite.token_manager --login-url"
        )
        return None

    return token


def _probe_token(token: str) -> bool:
    """
    Does the broker actually accept this token for the CURRENT api_key?

    /user/profile is the cheapest authenticated endpoint and, unlike /quote, it
    needs no market-data subscription — so a False here means the session is
    genuinely bad rather than the plan lacking an add-on.

    Network failures return True: an unreachable broker is not evidence that a
    token is invalid, and discarding a good session because the wifi dropped
    would force a pointless manual re-login.
    """
    import urllib.request, urllib.error, json
    req = urllib.request.Request(
        "https://api.kite.trade/user/profile",
        headers={"X-Kite-Version": "3",
                 "Authorization": f"token {KITE_API_KEY}:{token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            return e.code not in (400, 403)
        return body.get("error_type") != "TokenException"
    except Exception:
        return True


def is_token_valid() -> bool:
    return get_access_token() is not None


def token_status() -> dict:
    """Structured status for the morning brief and the data-quality monitor."""
    token    = _get_config(TOKEN_KEY)
    issued_s = _get_config(TOKEN_DATE_KEY)
    valid    = is_token_valid()
    return {
        "configured": bool(KITE_API_KEY and KITE_API_SECRET),
        "has_token":  bool(token),
        "issued_at":  issued_s,
        "valid":      valid,
        "login_url":  get_login_url() if KITE_API_KEY else None,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeOS v7 — Kite token manager")
    ap.add_argument("--login-url", action="store_true", help="Print the Zerodha login URL")
    ap.add_argument("--exchange",  metavar="REQUEST_TOKEN",
                    help="Exchange a request_token for an access_token")
    ap.add_argument("--status",    action="store_true", help="Show current token status")
    args = ap.parse_args()

    if args.login_url:
        print("\n" + "=" * 72)
        print("  STEP 1  Open this URL and log in to Zerodha")
        print("=" * 72)
        print(f"\n  {get_login_url()}\n")
        print("=" * 72)
        print("  STEP 2  Copy the URL you land on")
        print("=" * 72)
        print("\n  After login your browser is redirected to your registered")
        print("  redirect URL, and the address bar will look like:\n")
        print("    https://your-redirect/?action=login&status=success"
              "&request_token=Xy7Kp2mNq...\n")
        print("=" * 72)
        print("  STEP 3  Paste that WHOLE URL back here (quoted)")
        print("=" * 72)
        print("\n  python -m kite.token_manager --exchange \"<paste the full URL>\"\n")
        print("  The token is extracted for you. Pasting just the token works too.")
        print("  It is single-use and expires within minutes, so do this promptly.\n")
    elif args.exchange:
        exchange_request_token(args.exchange)
    elif args.status:
        st = token_status()
        for k, v in st.items():
            print(f"  {k:<12} {v}")
    else:
        ap.print_help()
