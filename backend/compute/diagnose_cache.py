"""
Diagnose why price_history_yf cache check returns 0 sufficient
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, logger

sb         = get_supabase()
today      = "2026-04-17"  # last trading day
today_date = datetime.strptime(today, "%Y-%m-%d").date()
cutoff     = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=470)).date())

print(f"today:  {today}")
print(f"cutoff: {cutoff}")

# Step 1 — how many total rows in table?
total = sb.table("price_history_yf").select("symbol", count="exact").execute()
print(f"\nTotal rows in price_history_yf: {total.count}")

# Step 2 — what date range exists?
oldest = sb.table("price_history_yf").select("date").order("date", desc=False).limit(1).execute().data
newest = sb.table("price_history_yf").select("date").order("date", desc=True).limit(1).execute().data
print(f"Date range: {oldest[0]['date']} → {newest[0]['date']}")

# Step 3 — test the exact query used in fetch_bulk_history_yf for 3 symbols
test_symbols = ["ABB", "RELIANCE", "TCS"]
print(f"\nTesting cache query for {test_symbols}...")

rows = (sb.table("price_history_yf")
          .select("symbol,date")
          .in_("symbol", test_symbols)
          .gte("date", cutoff)
          .lte("date", today)
          .order("date", desc=True)
          .limit(50000)
          .execute().data)

print(f"  Rows returned: {len(rows)}")
if rows:
    print(f"  Sample: {rows[:3]}")
else:
    print("  NO ROWS RETURNED — investigating why...")

    # Step 4 — check without date filter
    rows_no_date = (sb.table("price_history_yf")
                      .select("symbol,date")
                      .in_("symbol", test_symbols)
                      .execute().data)
    print(f"  Without date filter: {len(rows_no_date)} rows")
    if rows_no_date:
        print(f"  Sample dates: {[r['date'] for r in rows_no_date[:5]]}")

    # Step 5 — check single symbol without any filter
    rows_single = (sb.table("price_history_yf")
                     .select("symbol,date")
                     .eq("symbol", "ABB")
                     .order("date", desc=True)
                     .limit(5)
                     .execute().data)
    print(f"\n  ABB rows (no filter): {len(rows_single)}")
    if rows_single:
        print(f"  ABB dates: {[r['date'] for r in rows_single]}")
    else:
        print("  ABB has NO rows at all in price_history_yf!")

    # Step 6 — check what symbols ARE in the table
    sample_syms = (sb.table("price_history_yf")
                     .select("symbol")
                     .limit(10)
                     .execute().data)
    print(f"\n  Sample symbols in table: {[r['symbol'] for r in sample_syms]}")

# Step 7 — test sufficiency logic manually for ABB
print(f"\nManual sufficiency check for ABB:")
abb_rows = (sb.table("price_history_yf")
              .select("date")
              .eq("symbol", "ABB")
              .gte("date", cutoff)
              .lte("date", today)
              .order("date", desc=True)
              .limit(50000)
              .execute().data)
print(f"  Rows in date range: {len(abb_rows)}")
if abb_rows:
    most_recent = datetime.strptime(abb_rows[0]["date"], "%Y-%m-%d").date()
    days_stale  = (today_date - most_recent).days
    print(f"  Most recent date: {abb_rows[0]['date']}")
    print(f"  Days stale: {days_stale}")
    print(f"  MIN_SESSIONS_REQUIRED: 20")
    print(f"  Sufficient: {len(abb_rows) >= 20 and days_stale <= 5}")
