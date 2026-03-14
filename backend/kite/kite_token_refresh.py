"""
TradeOS v6 — Kite Token Refresh (Automated)
Logs into Zerodha headlessly using TOTP, generates access_token,
and pushes it to GitHub Secrets so all pipelines pick it up automatically.

Usage: python kite/kite_token_refresh.py
Wire:  pipeline_token_refresh.yml — runs at 8:30 AM IST daily, Mon–Fri
"""
import os
import sys
import requests
import pyotp
from base64 import b64encode
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import KITE_API_KEY, KITE_API_SECRET, DATA, logger

# ── Secrets (injected via GitHub Secrets or .env) ─────────────────────────────
KITE_USER_ID      = os.getenv("KITE_USER_ID", "")
KITE_PASSWORD     = os.getenv("KITE_PASSWORD", "")
KITE_TOTP_SECRET  = os.getenv("KITE_TOTP_SECRET", "")   # Base32 seed from authenticator
GH_PAT_TOKEN      = os.getenv("GH_PAT_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")  # e.g. username/tradeos-v6


def fetch_access_token() -> str:
    """
    Fully headless Zerodha login using TOTP.
    Returns a fresh access_token valid for the day.
    """
    from kiteconnect import KiteConnect

    session = requests.Session()

    # Step 1: Password login
    logger.info("Kite token refresh: starting login...")
    login_resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": KITE_USER_ID, "password": KITE_PASSWORD},
    )
    login_resp.raise_for_status()
    request_id = login_resp.json()["data"]["request_id"]
    logger.info("Step 1 ✅ — password login successful")

    # Step 2: TOTP 2FA
    totp_code = pyotp.TOTP(KITE_TOTP_SECRET).now()
    twofa_resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":     KITE_USER_ID,
            "request_id":  request_id,
            "twofa_value": totp_code,
            "twofa_type":  "totp",
        },
    )
    twofa_resp.raise_for_status()
    logger.info("Step 2 ✅ — TOTP 2FA successful")

    # Step 3: Hit Kite login URL to get request_token via redirect
    kite = KiteConnect(api_key=KITE_API_KEY)
    login_url = kite.login_url()
    redirect_resp = session.get(login_url, allow_redirects=False)
    location = redirect_resp.headers.get("Location", "")

    if "request_token=" not in location:
        raise ValueError(f"No request_token in redirect. Got: {location}")

    request_token = location.split("request_token=")[1].split("&")[0]
    logger.info("Step 3 ✅ — request_token obtained")

    # Step 4: Exchange for access_token
    session_data  = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    access_token  = session_data["access_token"]
    logger.info("Step 4 ✅ — access_token generated")

    # Save locally too (for local dev use)
    token_file = DATA / "kite_token.txt"
    date_file  = DATA / "kite_token_date.txt"
    token_file.write_text(access_token)
    date_file.write_text(date.today().isoformat())

    return access_token


def push_to_github_secrets(secret_name: str, secret_value: str):
    """
    Encrypts and pushes a secret to GitHub repository secrets
    using the repo's public key (required by GitHub API).
    """
    try:
        from nacl import encoding, public
    except ImportError:
        logger.error("PyNaCl not installed. Run: pip install PyNaCl")
        raise

    headers = {
        "Authorization": f"Bearer {GH_PAT_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"

    # Fetch repo public key for encryption
    key_resp = requests.get(f"{base_url}/actions/secrets/public-key", headers=headers)
    key_resp.raise_for_status()
    key_data = key_resp.json()

    # Encrypt using LibSodium sealed box
    pub_key   = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    sealed    = public.SealedBox(pub_key).encrypt(secret_value.encode())
    encrypted = b64encode(sealed).decode()

    # Upsert the secret
    put_resp = requests.put(
        f"{base_url}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
    )
    put_resp.raise_for_status()
    logger.info(f"✅ GitHub Secret '{secret_name}' updated successfully")


def main():
    missing = [v for v in [KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET,
                            KITE_API_KEY, KITE_API_SECRET] if not v]
    if missing:
        logger.error(f"Missing required env vars — check .env or GitHub Secrets")
        sys.exit(1)

    try:
        access_token = fetch_access_token()
    except Exception as e:
        logger.error(f"❌ Token generation failed: {e}")
        sys.exit(1)

    # Push to GitHub Secrets if PAT and repo are configured
    if GH_PAT_TOKEN and GITHUB_REPOSITORY:
        try:
            push_to_github_secrets("KITE_ACCESS_TOKEN", access_token)
        except Exception as e:
            logger.warning(f"GitHub push failed: {e} — token saved locally only")
    else:
        logger.warning("GH_PAT_TOKEN or GITHUB_REPOSITORY not set — skipping GitHub push")
        logger.info(f"Add to .env manually: KITE_ACCESS_TOKEN={access_token}")

    logger.info(f"Token refresh complete for {date.today().isoformat()}")
    return {"status": "ok", "date": date.today().isoformat()}


if __name__ == "__main__":
    main()