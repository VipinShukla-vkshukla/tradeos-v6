"""
TradeOS v6 — Kite Access Token Manager
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

    try:
        issued = datetime.fromisoformat(issued_s)
        if issued.tzinfo is None:
            issued = IST.localize(issued)
    except Exception:
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
    ap = argparse.ArgumentParser(description="TradeOS v6 — Kite token manager")
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
