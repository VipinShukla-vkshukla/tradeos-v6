"""
TradeOS v6 — Chartink Atlas CSV Fetcher (Playwright)
Fetches CSV from Chartink dashboard and writes to Google Sheet + Supabase.
Runs headed (headless=False) always — Xvfb provides virtual display on GitHub Actions.
"""
import io
import os
import sys
import time
import math
import re
import pandas as pd
from datetime import datetime
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS, get_supabase

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
import requests as req_lib

CHARTINK_EMAIL    = os.getenv("CHARTINK_EMAIL", "")
CHARTINK_PASSWORD = os.getenv("CHARTINK_PASSWORD", "")
DASHBOARD_URL     = "https://chartink.com/dashboard/397664"
SHEET_TAB         = "Chartink Raw Data_Nifty 500"
TARGET_WIDGET     = "Comprehensive Strategy Export"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheets_service():
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_widget_box(page):
    return page.evaluate(f"""
        () => {{
            const target = "{TARGET_WIDGET}";
            const headings = [...document.querySelectorAll('*')]
                .filter(el =>
                    el.childElementCount === 0 &&
                    el.textContent.trim() === target
                );
            if (!headings.length) return null;
            const h = headings[headings.length - 1];
            let node = h;
            for (let i = 0; i < 12; i++) {{
                node = node.parentElement;
                if (!node) break;
                const r = node.getBoundingClientRect();
                if (r.width > 200 && r.height > 100) {{
                    return {{ x: r.x, y: r.y, width: r.width, height: r.height }};
                }}
            }}
            return null;
        }}
    """)


def fetch_chartink_csv() -> pd.DataFrame | None:
    with sync_playwright() as p:
        # Always headed — Xvfb provides virtual display on GitHub Actions
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()
        page.set_viewport_size({"width": 1400, "height": 900})

        try:
            # ── Step 1: Login ──────────────────────────────────────────────────
            logger.info("Navigating to Chartink login...")
            page.goto("https://chartink.com/login", wait_until="networkidle")
            page.fill('input[name="email"], input[type="email"]', CHARTINK_EMAIL)
            page.fill('input[name="password"], input[type="password"]', CHARTINK_PASSWORD)
            page.click('button:has-text("Log in")')
            page.wait_for_load_state("networkidle")
            logger.info("✓ Login submitted")

            # ── Step 2: Navigate to dashboard ─────────────────────────────────
            logger.info("Navigating to Swing dashboard...")
            page.goto(DASHBOARD_URL, wait_until="networkidle")
            logger.info("Waiting for widgets to render...")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(8)

            # ── Step 3: Dismiss any popups ────────────────────────────────────
            try:
                ok_btn = page.locator("button:has-text('Ok'), button:has-text('OK'), button:has-text('ok')")
                if ok_btn.count() > 0:
                    ok_btn.first.click()
                    logger.info("Dismissed popup")
                    time.sleep(1)
            except Exception:
                pass
            page.keyboard.press("Escape")
            time.sleep(0.5)
            
            # Check if widgets loaded — look for error messages
            error_widgets = page.locator("text=Error while loading").count()
            if error_widgets > 0:
                logger.warning(f"{error_widgets} widgets failed to load — retrying page...")
                page.reload(wait_until="networkidle")
                time.sleep(10)  # wait longer after reload
                # Dismiss popup again if present
                try:
                    ok_btn = page.locator("button:has-text('Ok'), button:has-text('OK')")
                    if ok_btn.count() > 0:
                        ok_btn.first.click()
                        time.sleep(1)
                except Exception:
                    pass
                page.keyboard.press("Escape")
                time.sleep(0.5)
            
            # ── Step 4: Get widget box & scroll header near top of viewport ───
            box = get_widget_box(page)
            if not box:
                logger.error("Widget not found")
                page.screenshot(path="chartink_debug.png")
                return None
            logger.info(f"Widget box: {box}")

            page.evaluate(f"window.scrollTo(0, {max(0, box['y'] - 150)})")
            time.sleep(1)
            box = get_widget_box(page)
            logger.info(f"Widget box after scroll: {box}")

            # ── Step 5: Glide mouse → header title → CSV button ───────────────
            title_x = box["x"] + 60
            title_y = box["y"] + 15
            csv_x   = box["x"] + box["width"] - 50
            csv_y   = box["y"] + 15

            logger.info(f"Moving mouse: neutral → title ({title_x:.0f},{title_y:.0f}) → CSV ({csv_x:.0f},{csv_y:.0f})")
            page.mouse.move(400, 50, steps=5)
            time.sleep(0.2)
            page.mouse.move(title_x, title_y, steps=30)
            time.sleep(2)
            page.mouse.move(csv_x, csv_y, steps=30)
            time.sleep(1)

            # ── Step 6: Click & capture download ──────────────────────────────
            logger.info("Clicking CSV button...")
            with page.expect_download(timeout=15000) as dl_info:
                page.mouse.click(csv_x, csv_y)

            download = dl_info.value
            df = pd.read_csv(download.path())
            logger.info(f"✓ CSV downloaded: {len(df)} rows")
            return df

        except Exception as outer_ex:
            logger.warning(f"Direct click failed: {outer_ex}")
            page.screenshot(path="chartink_hover_debug.png")
            logger.error("All strategies failed. Screenshot saved.")
            return None

        finally:
            try:
                browser.close()
            except Exception:
                pass


def write_to_sheet(service, df: pd.DataFrame):
    """Keeps header in row 1 untouched. Clears data from row 2 onwards and rewrites."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    tab      = SHEET_TAB

    logger.info(f"Writing {len(df)} rows to '{tab}'...")
    df = df.fillna("")
    rows = df.values.tolist()

    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A2:ZZ10000"
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A2",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()

    logger.info(f"✓ Written to '{tab}': {len(df)} rows, {len(df.columns)} columns")


def upsert_to_supabase(df: pd.DataFrame):
    """Appends today's Chartink data to Supabase. UNIQUE(date,symbol) prevents duplicates."""
    sb = get_supabase()
    logger.info(f"Upserting {len(df)} rows to Supabase chartink_raw_data...")

    col_map = {
        "Date": "date", "Symbol": "symbol", "Sector": "sector",
        "Industry": "industry", "Market cap": "market_cap",
        "Market cap category": "market_cap_cat",
        "Daily open": "daily_open", "Daily high": "daily_high",
        "Daily low": "daily_low", "Daily close": "daily_close",
        "52 weeks high": "week52_high", "52 weeks low": "week52_low",
        "30days highest high": "high_30d",
        "10 sma": "sma_10", "20 sma": "sma_20",
        "50 sma": "sma_50", "200 sma": "sma_200",
        "10 ema": "ema_10", "20 ema": "ema_20", "50 ema": "ema_50",
        "Rsi daily": "rsi_daily", "Rsi weekly": "rsi_weekly",
        "Rsi monthly": "rsi_monthly", "Adx 14": "adx_14",
        "Adx +di": "adx_plus_di", "Adx -di": "adx_minus_di",
        "Volume": "volume", "20 avg volume": "avg_vol_20",
        "50 avg volume": "avg_vol_50", "Daily vwap": "vwap_daily",
        "20 day vwap": "vwap_20d", "50 day vwap": "vwap_50d",
        "% change": "pct_change", "Atr 14": "atr_14", "Atr%": "atr_pct",
        "Heikin-ashi high": "ha_high", "Heikin-ashi low": "ha_low",
        "Heikin-ashi close": "ha_close", "Supertrend": "supertrend",
        "Macd line": "macd_line", "Macd signal": "macd_signal",
        "Macd histogram": "macd_histogram", "Parabolic sar": "parabolic_sar",
        "Upper bollinger": "upper_bb", "Lower bollinger": "lower_bb",
        "Stochastic": "stochastic", "Ttm net profit": "ttm_net_profit",
        "Net profit yearly": "net_profit_yr", "Eps": "eps",
        "Quarterly net profit": "qtr_net_profit",
        "Quarterly variance net profit": "qtr_var_profit",
    }

    df = df.rename(columns=col_map)

    def parse_chartink_date(raw: str) -> str:
        """Convert "4th Mar'26" → "2026-03-04" """
        try:
            clean_d = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', raw.strip())
            clean_d = clean_d.replace("'", " 20")
            return datetime.strptime(clean_d.strip(), "%d %b %Y").strftime("%Y-%m-%d")
        except Exception:
            from config import today_ist
            return str(today_ist())

    def clean(v):
        if v is None: return None
        if isinstance(v, float) and math.isnan(v): return None
        return v

    rows = []
    for r in df.to_dict("records"):
        row = {k: clean(v) for k, v in r.items()}
        if "date" in row and row["date"]:
            row["date"] = parse_chartink_date(str(row["date"]))
        rows.append(row)

    errors = 0
    for i in range(0, len(rows), 100):
        try:
            sb.table("chartink_raw_data").upsert(
                rows[i:i+100], on_conflict="date,symbol"
            ).execute()
        except Exception as e:
            logger.error(f"Supabase upsert batch {i//100} failed: {e}")
            errors += 1

    if errors == 0:
        logger.info(f"✓ Upserted to chartink_raw_data: {len(rows)} rows")
    else:
        logger.warning(f"Upserted with {errors} batch errors")


def main():
    if not CHARTINK_EMAIL or not CHARTINK_PASSWORD:
        logger.error("CHARTINK_EMAIL and CHARTINK_PASSWORD must be set in .env")
        return 0

    df = fetch_chartink_csv()
    if df is None or df.empty:
        logger.error("No data fetched from Chartink")
        return 0

    service = get_sheets_service()
    write_to_sheet(service, df)
    upsert_to_supabase(df)

    return len(df)


if __name__ == "__main__":
    main()