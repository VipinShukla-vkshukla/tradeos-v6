# test_nse_pcr.py  — run standalone to verify NSE session without touching Supabase
import requests, time, json

_NSE_PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive", "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}
_NSE_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive", "Referer": "https://www.nseindia.com/option-chain",
    "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin", "X-Requested-With": "XMLHttpRequest",
}

session = requests.Session()
print("Step 1: Visiting homepage...")
r1 = session.get("https://www.nseindia.com", headers=_NSE_PAGE_HEADERS, timeout=15)
print(f"  → HTTP {r1.status_code} | cookies set: {len(session.cookies)}")
time.sleep(2)

print("Step 2: Visiting option-chain page...")
r2 = session.get("https://www.nseindia.com/option-chain", headers=_NSE_PAGE_HEADERS, timeout=15)
print(f"  → HTTP {r2.status_code} | cookies set: {len(session.cookies)}")
time.sleep(1)

print("Step 3: Calling API...")
r3 = session.get(
    "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
    headers=_NSE_API_HEADERS, timeout=20,
)
print(f"  → HTTP {r3.status_code} | Content-Type: {r3.headers.get('Content-Type','')}")

if r3.status_code == 200:
    data = r3.json()
    f = data.get("filtered", {})
    put_oi  = f.get("PE", {}).get("totOI", 0)
    call_oi = f.get("CE", {}).get("totOI", 0)
    pcr = round(put_oi / call_oi, 3) if call_oi else 0
    print(f"\n✅ PCR = {pcr}  (put_oi={put_oi:,} / call_oi={call_oi:,})")
else:
    print(f"\n❌ Failed — response body: {r3.text[:300]}")

session.close()