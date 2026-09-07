-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 130: shrink stock_data_daily and raw_prices/chartink_raw_data
-- retention now that their historical readers moved to price_history_yf
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 08-Sep-2026. Migrations 016/030 set stock_data_daily's retention at 250
-- TRADING days because "the 200-day moving average is the longest lookback
-- anything reads" -- checked against the actual code this session: sma_200/
-- sma_50/atr_14/supertrend are Chartink vendor passthroughs
-- (compute_indicators.py PASSTHROUGH_FIELDS), never a rolling computation
-- over stock_data_daily's own rows. Nothing computes them from history at
-- all, so that justification described code that never existed.
--
-- The REAL longest reader of stock_data_daily's own historical rows was
-- allocation/outcomes.py at 120 CALENDAR days, reading only high/low/close
-- -- never sector/asm_flag/delivery_pct/value_cr/market_cap, which every
-- historical reader only ever wants from TODAY's row. Five call sites
-- (control/exit_rules.py, allocation/outcomes.py, swing/signals/outcomes.py,
-- swing/brain/performance_tracker.py, swing/brain/data_aggregator.py) moved
-- to price_history_yf this session, which already carries the same OHLCV
-- columns back to 2025-01-02.
--
-- VERIFIED LIVE BEFORE THIS SHIPPED, not assumed:
--   - today's active ~500-symbol universe has IDENTICAL per-symbol row
--     counts in stock_data_daily and price_history_yf over the last 120
--     days (0 short, 0 missing).
--   - close prices agree EXACTLY over that same window: 40,991 symbol-days
--     compared, max abs diff 0.0000.
--   - an unbounded comparison DID find real divergence -- 87 symbols,
--     clustered entirely in stock_data_daily's own first ~7 weeks
--     (2026-03-06 through 2026-04-29), a data-quality artifact of its early
--     bring-up, not a live risk -- more than 120 days old and never read by
--     anything. tools/health.py::check_price_source_parity() now watches
--     for this going forward, on whatever window stock_data_daily still
--     retains, so a FUTURE divergence (a real corporate action retroactively
--     adjusted by price_history_yf's own auto_adjust=True yfinance fetch,
--     confirmed present in compute_indicators.py) is caught the day it
--     first appears rather than silently corrupting a price-level
--     comparison. See docs/FINDINGS.md, 08-Sep-2026.
--
-- keep_days moves from 250 trading days to 8 -- a safety buffer for
-- reconciliation and rollback, not a number any live consumer needs
-- anymore. archive_stock_data() (migration 016) is unchanged: still
-- archive-then-delete, still keeps the 9-column OHLCV+ATR slim copy in
-- stock_data_archive before deleting.
--
-- raw_prices/chartink_raw_data: migration 032 already found ZERO historical
-- readers and set their shared retention at 120 calendar days purely as
-- re-run headroom. Confirmed again this session, live: every read of either
-- table is `.eq("date", today)` or `.order("date", desc=True).limit(1)` --
-- nothing wants history. Both re-derivable from source bhavcopy/Chartink
-- exports (migration 032's own reasoning), so this stays delete-only, no
-- archive step. keep_days moves from 120 to 8, matching stock_data_daily's
-- own new buffer.

UPDATE public.system_config
SET value = '8',
    description = 'Trading days of full 86-column detail kept in '
      'stock_data_daily. Anything older is copied to stock_data_archive '
      '(9 columns) then deleted. Was 250, set for a 200-day moving average '
      'that turned out not to be computed from this table (sma_200 is a '
      'Chartink passthrough). The real longest reader (allocation/'
      'outcomes.py, 120 calendar days) moved to price_history_yf on '
      '08-Sep-2026 -- see docs/FINDINGS.md. 8 is a reconciliation/rollback '
      'buffer, not a live requirement.'
WHERE key = 'storage_rolloff_keep_days';

UPDATE public.system_config
SET value = '8',
    description = 'Calendar days kept in raw_prices and chartink_raw_data '
      'before delete (no archive -- both re-derivable from source '
      'bhavcopy/Chartink exports). Was 120; every live reader of either '
      'table only ever wants today''s row (migration 032 already found '
      'this, confirmed again 08-Sep-2026 -- see docs/FINDINGS.md). 8 is '
      're-run headroom, not a live requirement.'
WHERE key = 'storage_staging_keep_days';
