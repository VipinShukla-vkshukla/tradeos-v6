"""
TradeOS v6 — Kite Token Refresh
Run this once every morning before market hours.
Opens browser for Zerodha login, saves access token.

Usage: python kite/kite_token_refresh.py
"""
import sys
import webbrowser
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import KITE_API_KEY, KITE_API_SECRET, DATA, logger

def main():
    if not KITE_API_KEY or not KITE_API_SECRET:
        print("ERROR: KITE_API_KEY and KITE_API_SECRET must be set in .env")
        print("Get them from: https://developers.kite.trade/apps")
        sys.exit(1)

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        print("ERROR: kiteconnect not installed. Run: pip install kiteconnect")
        sys.exit(1)

    kite = KiteConnect(api_key=KITE_API_KEY)
    login_url = kite.login_url()

    print("=" * 60)
    print("TradeOS v6 — Kite Token Refresh")
    print("=" * 60)
    print(f"\nOpening Zerodha login in browser...")
    print(f"URL: {login_url}\n")
    webbrowser.open(login_url)

    print("After logging in, Zerodha will redirect to a URL like:")
    print("  https://127.0.0.1/?request_token=XXXXXX&action=login&status=success")
    print()
    request_token = input("Paste the request_token from the URL: ").strip()

    try:
        session = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = session["access_token"]

        # Save to files and .env update note
        token_file = DATA / "kite_token.txt"
        date_file  = DATA / "kite_token_date.txt"
        token_file.write_text(access_token)
        date_file.write_text(date.today().isoformat())

        print(f"\n✅ Token saved successfully!")
        print(f"   Valid for: {date.today().isoformat()}")
        print(f"\nAdd to .env: KITE_ACCESS_TOKEN={access_token}")
        print("\nThe pipeline will use this token automatically today.")
        return access_token
    except Exception as e:
        print(f"\n❌ Token generation failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
