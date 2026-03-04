"""
TradeOS v6 — Chartink Atlas CSV Fetcher
Logs into Chartink, downloads Comprehensive Strategy Export CSV,
and writes it to Google Sheet tab: Chartink Raw Data_Nifty 500
"""
import io
import time
import requests
import pandas as pd
from loguru import logger
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import os
import json

CHARTINK_EMAIL    = os.getenv("CHARTINK_EMAIL", "")
CHARTINK_PASSWORD = os.getenv("CHARTINK_PASSWORD", "")
DASHBOARD_ID      = "397664"
SHEET_TAB         = "Chartink Raw Data_Nifty 500"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_sheets_service():
    creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials/service_account.json")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

def chartink_login(session: requests.Session) -> bool:
    """Login to Chartink and return True if successful."""
    logger.info("Logging into Chartink...")
    
    # Get CSRF token first
    r = session.get("https://chartink.com/login")
    if r.status_code != 200:
        logger.error("Failed to reach Chartink login page")
        return False

    # Extract CSRF token from page
    import re
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    if not csrf:
        logger.error("Could not find CSRF token")
        return False
    
    csrf_token = csrf.group(1)

    # Login
    login_r = session.post(
        "https://chartink.com/login",
        data={
            "_token": csrf_token,
            "email": CHARTINK_EMAIL,
            "password": CHARTINK_PASSWORD,
        },
        headers={"Referer": "https://chartink.com/login"}
    )
    
    if "logout" in login_r.text.lower() or login_r.url != "https://chartink.com/login":
        logger.info("✓ Chartink login successful")
        return True
    else:
        logger.error("✗ Chartink login failed — check credentials")
        return False

def get_widget_scan_id(session: requests.Session, dashboard_id: str) -> str | None:
    """Fetch dashboard and find Comprehensive Strategy Export widget scan ID."""
    logger.info(f"Fetching dashboard {dashboard_id}...")
    
    r = session.get(f"https://chartink.com/dashboard/{dashboard_id}")
    if r.status_code != 200:
        logger.error(f"Failed to fetch dashboard: {r.status_code}")
        return None

    # Find scan ID for Comprehensive Strategy Export
    import re
    # Look for the widget data in the page
    matches = re.findall(r'"scan_id"\s*:\s*(\d+)[^}]*"name"\s*:\s*"([^"]*Comprehensive[^"]*)"', r.text, re.IGNORECASE)
    if not matches:
        # Try alternate pattern
        matches = re.findall(r'"name"\s*:\s*"([^"]*Comprehensive[^"]*)"[^}]*"scan_id"\s*:\s*(\d+)', r.text, re.IGNORECASE)
        if matches:
            scan_id = matches[0][1]
        else:
            logger.error("Could not find Comprehensive Strategy Export widget")
            return None
    else:
        scan_id = matches[0][0]
    
    logger.info(f"✓ Found scan ID: {scan_id}")
    return scan_id

def download_csv(session: requests.Session, scan_id: str) -> pd.DataFrame | None:
    """Download CSV for the given scan ID."""
    logger.info(f"Downloading CSV for scan {scan_id}...")
    
    # Chartink CSV download endpoint
    r = session.get(
        f"https://chartink.com/screener/scan-results/download",
        params={"scan_clause": scan_id, "format": "csv"},
        headers={"Referer": f"https://chartink.com/dashboard/{DASHBOARD_ID}"}
    )
    
    if r.status_code != 200 or not r.content:
        # Try alternate endpoint
        r = session.post(
            "https://chartink.com/screener/process",
            data={"scan_clause": scan_id, "download": "csv"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://chartink.com/dashboard/{DASHBOARD_ID}"
            }
        )
    
    if r.status_code != 200:
        logger.error(f"CSV download failed: {r.status_code}")
        return None
    
    try:
        df = pd.read_csv(io.StringIO(r.text))
        logger.info(f"✓ Downloaded {len(df)} rows")
        return df
    except Exception as e:
        logger.error(f"Failed to parse CSV: {e}")
        return None

def write_to_sheet(service, df: pd.DataFrame, sheet_id: str, tab: str):
    """Write DataFrame to Google Sheet, replacing existing data from row 1."""
    logger.info(f"Writing {len(df)} rows to '{tab}'...")
    
    # Convert DataFrame to list of lists
    headers = [df.columns.tolist()]
    rows    = df.values.tolist()
    values  = headers + rows

    # Clear existing content first
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1:Z10000"
    ).execute()

    # Write new data
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        body={"values": values}
    ).execute()

    logger.info(f"✓ Written to '{tab}': {len(df)} rows, {len(df.columns)} columns")

def main():
    from config import GOOGLE_SHEET_ID

    if not CHARTINK_EMAIL or not CHARTINK_PASSWORD:
        logger.error("CHARTINK_EMAIL and CHARTINK_PASSWORD must be set")
        return 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    # Step 1 — Login
    if not chartink_login(session):
        return 0

    # Step 2 — Get scan ID from dashboard
    scan_id = get_widget_scan_id(session, DASHBOARD_ID)
    if not scan_id:
        return 0

    # Step 3 — Download CSV
    df = download_csv(session, scan_id)
    if df is None or df.empty:
        return 0

    # Step 4 — Write to Google Sheet
    service = get_sheets_service()
    write_to_sheet(service, df, GOOGLE_SHEET_ID, SHEET_TAB)

    return len(df)

if __name__ == "__main__":
    main()