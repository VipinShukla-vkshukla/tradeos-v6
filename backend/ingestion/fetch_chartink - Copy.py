"""
TradeOS v6 — Chartink Atlas CSV Fetcher (Playwright)
Fetches CSV from Chartink dashboard and writes to Google Sheet.
"""
import io
import os
import sys
import time
import pandas as pd
from loguru import logger
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS
from config import get_supabase
supabase = get_supabase()
import re
from datetime import datetime
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
        headless = os.getenv("CI", "false").lower() == "true"
        browser = p.chromium.launch(headless=headless)
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
            time.sleep(3)

            # ── Step 3: Dismiss any popups ────────────────────────────────────
            page.keyboard.press("Escape")
            time.sleep(0.3)

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

            # ── Step 5: Attach network interceptor ────────────────────────────
            csv_urls = []
            def on_response(response):
                url = response.url.lower()
                ct  = (response.headers.get("content-type") or "").lower()
                if "csv" in url or "text/csv" in ct or "download" in url:
                    csv_urls.append(response.url)
                    logger.info(f"[NET] {response.url}")
            page.on("response", lambda res: logger.info(f"[RES] {res.status} {res.url} | CT: {res.headers.get('content-type','')}") if "chartink" in res.url else None)
            page.on("response", on_response)

            def on_all_requests(request):
                if any(k in request.url.lower() for k in ["csv", "export", "download", "widget"]):
                    logger.info(f"[REQ] {request.method} {request.url}")
            page.on("request", on_all_requests)

             # ── Step 6: Headless-safe CSV trigger ─────────────────────────────
            # In headless mode, mouse hover doesn't trigger CSS :hover reliably.
            # Strategy: JS dispatchEvent to force hover state, then find & click
            # the CSV button directly without relying on physical mouse movement.

            if headless:
                logger.info("Headless mode: using JS dispatchEvent strategy...")

                # Fire hover events on widget container via JS
                page.evaluate(f"""
                    () => {{
                        const target = "{TARGET_WIDGET}";
                        const headings = [...document.querySelectorAll('*')]
                            .filter(el =>
                                el.childElementCount === 0 &&
                                el.textContent.trim() === target
                            );
                        if (!headings.length) return;
                        const h = headings[headings.length - 1];
                        let container = h;
                        for (let i = 0; i < 12; i++) {{
                            container = container.parentElement;
                            if (!container) break;
                            const r = container.getBoundingClientRect();
                            if (r.width > 200 && r.height > 100) break;
                        }}
                        ['mouseenter','mouseover','mousemove','pointerenter','pointerover']
                            .forEach(t => container.dispatchEvent(
                                new MouseEvent(t, {{bubbles:true, cancelable:true, view:window}})
                            ));
                    }}
                """)
                time.sleep(2)

                # Poll for CSV button up to 15s
                csv_btn_coords = None
                for attempt in range(30):
                    coords = page.evaluate(f"""
                        () => {{
                            const target = "{TARGET_WIDGET}";
                            const headings = [...document.querySelectorAll('*')]
                                .filter(el =>
                                    el.childElementCount === 0 &&
                                    el.textContent.trim() === target
                                );
                            if (!headings.length) return null;
                            const h = headings[headings.length - 1];
                            let container = h;
                            for (let i = 0; i < 12; i++) {{
                                container = container.parentElement;
                                if (!container) break;
                                const r = container.getBoundingClientRect();
                                if (r.width > 200 && r.height > 100) break;
                            }}
                            const btn = [...container.querySelectorAll('a,button')]
                                .find(el => {{
                                    const r = el.getBoundingClientRect();
                                    return r.width > 0 && r.height > 0 && (
                                        /csv/i.test(el.textContent.trim()) ||
                                        /csv/i.test(el.title || '') ||
                                        /csv/i.test(el.href || '')
                                    );
                                }});
                            if (!btn) return null;
                            const r = btn.getBoundingClientRect();
                            return {{ x: r.x + r.width/2, y: r.y + r.height/2 }};
                        }}
                    """)
                    if coords and coords.get("x", 0) > 0:
                        csv_btn_coords = coords
                        logger.info(f"✓ CSV button found at attempt {attempt}: {coords}")
                        break
                    # Re-fire hover events every 3 attempts
                    if attempt % 3 == 2:
                        page.evaluate(f"""
                            () => {{
                                const target = "{TARGET_WIDGET}";
                                const h = [...document.querySelectorAll('*')]
                                    .filter(el => el.childElementCount === 0 && el.textContent.trim() === target)
                                    .pop();
                                if (!h) return;
                                let c = h;
                                for (let i = 0; i < 12; i++) {{
                                    c = c.parentElement;
                                    if (!c) break;
                                    const r = c.getBoundingClientRect();
                                    if (r.width > 200 && r.height > 100) break;
                                }}
                                ['mouseenter','mouseover','mousemove']
                                    .forEach(t => c.dispatchEvent(
                                        new MouseEvent(t, {{bubbles:true, cancelable:true, view:window}})
                                    ));
                            }}
                        """)
                    time.sleep(0.5)

                if not csv_btn_coords:
                    logger.error("CSV button not found in headless mode")
                    page.screenshot(path="chartink_headless_debug.png")
                    return None

                logger.info("Clicking CSV button via coordinates...")
                
                with page.expect_download(timeout=15000) as dl_info:
                    page.mouse.click(csv_btn_coords["x"], csv_btn_coords["y"])

            else:
                # ── Headed mode (local): physical mouse glide ──────────────────
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

                # Capture the CSV href before clicking
            csv_href = page.evaluate(f"""
                () => {{
                    const target = "{TARGET_WIDGET}";
                    const headings = [...document.querySelectorAll('*')]
                        .filter(el => el.childElementCount === 0 && el.textContent.trim() === target);
                    if (!headings.length) return null;
                    const h = headings[headings.length - 1];
                    let container = h;
                    for (let i = 0; i < 12; i++) {{
                        container = container.parentElement;
                        if (!container) break;
                        const r = container.getBoundingClientRect();
                        if (r.width > 200 && r.height > 100) break;
                    }}
                    const btn = [...container.querySelectorAll('a,button')]
                        .find(el => /csv/i.test(el.textContent.trim()) ||
                                   /csv/i.test(el.title||'') ||
                                   /csv/i.test(el.href||''));
                    return btn ? (btn.href || btn.getAttribute('href') || btn.outerHTML.slice(0,200)) : null;
                }}
            """)
            logger.info(f"[CSV HREF] {csv_href}")

            logger.info("Clicking CSV button...")
            with page.expect_download(timeout=10000) as dl_info:
                page.mouse.click(csv_x, csv_y)

            # ── Step 7: Read downloaded CSV ────────────────────────────────────
            download = dl_info.value
            df = pd.read_csv(download.path())
            logger.info(f"✓ CSV downloaded: {len(df)} rows")
            return df

        except Exception as outer_ex:
            logger.warning(f"Direct click failed: {outer_ex}")

            # ── Fallback: use intercepted network URL ──────────────────────────
            if csv_urls:
                logger.info(f"Using intercepted URL: {csv_urls[-1]}")
                cookies = context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                resp = req_lib.get(csv_urls[-1], headers={
                    "Cookie": cookie_str,
                    "Referer": DASHBOARD_URL,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }, timeout=30)
                resp.raise_for_status()
                df = pd.read_csv(io.StringIO(resp.text))
                logger.info(f"✓ CSV via network intercept: {len(df)} rows")
                return df

            page.screenshot(path="chartink_hover_debug.png")
            logger.error("All strategies failed. Screenshot saved.")
            return None

        finally:
            try:
                browser.close()
            except Exception:
                pass


def write_to_sheet(service, df: pd.DataFrame):
    """
    Clears the tab and rewrites header (row 1) + all data (row 2 onwards).
    Uses RAW input so numbers stay as numbers.
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    tab      = SHEET_TAB

    logger.info(f"Writing {len(df)} rows to '{tab}'...")

    # Replace NaN with empty string so Sheets API accepts it
    df = df.fillna("")

    headers = [df.columns.tolist()]          # row 1
    rows    = df.values.tolist()             # rows 2..N
    values  = headers + rows

    # Clear existing content first
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A2:ZZ1000"
    ).execute()

    # Write header + data in one call starting at A1
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        body={"values": values}
    ).execute()

    logger.info(f"✓ Written to '{tab}': {len(df)} rows, {len(df.columns)} columns")

def upsert_to_supabase(df: pd.DataFrame):
    """Appends today's Chartink data to Supabase. UNIQUE(date,symbol) prevents duplicates."""
    import math

    logger.info(f"Upserting {len(df)} rows to Supabase chartink_raw_data...")

    # Column mapping: CSV name → Supabase column
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

    # Clean NaN → None for Supabase
    import re
    from datetime import datetime

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

    # Upsert in batches of 100
    for i in range(0, len(rows), 100):
        supabase.table("chartink_raw_data").upsert(
            rows[i:i+100], on_conflict="date,symbol"
        ).execute()

    logger.info(f"✓ Upserted to chartink_raw_data: {len(rows)} rows")

def main():
    if not CHARTINK_EMAIL or not CHARTINK_PASSWORD:
        logger.error("CHARTINK_EMAIL and CHARTINK_PASSWORD must be set in .env")
        return 0

    df = fetch_chartink_csv()
    if df is None or df.empty:
        logger.error("No data fetched from Chartink")
        return 0

    # ── Debug print ───────────────────────────────────────────────────────────
    #print("\n" + "="*60)
    #print(f"COLUMNS : {list(df.columns)}")
    #print(f"TOTAL ROWS: {len(df)}")
    #print("\nFIRST 5 ROWS:")
    #print(df.head().to_string())
    #print("="*60 + "\n")

    # ── Write to Google Sheet ─────────────────────────────────────────────────
    service = get_sheets_service()
    write_to_sheet(service, df)
    upsert_to_supabase(df)

    return len(df)


if __name__ == "__main__":
    main()