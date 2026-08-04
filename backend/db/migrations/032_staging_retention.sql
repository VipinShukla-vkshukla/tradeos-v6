-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 032: retention for ingestion staging (Stage 1, decided)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Decided by the operator on 04-Aug-2026 against RETENTION_PROPOSAL.md, after
-- the one question that mattered was answered by measurement rather than by
-- argument.
--
-- THE QUESTION
-- ------------
--   "raw_prices and chartink_raw_data are used to calculate certain fields in
--    stock_data_daily. Are we not using them anywhere?"
--
-- Correct premise, and the right thing to be careful about. The answer is that
-- they are used ONCE, on the day, and the result is persisted. Verified on the
-- live database:
--
--     date          rows   delivery_pct   value_cr   sma_200   ret_12m
--     2026-03-09     400            398          0       400       398
--     2026-05-15     400            399        399       387       379
--     2026-07-01     400            400        400       388       383
--     2026-08-03     400            400        400       388       382
--
-- Every field derived from staging is sitting in stock_data_daily, populated,
-- months after the staging row was written. And every read of either staging
-- table anywhere in the repository is `.eq("date", today)` — one day deep:
--
--     compute_indicators.py:668   raw_prices        .eq("date", today)
--     compute_indicators.py:558   chartink_raw_data .eq("date", today)
--     data_quality_monitor        max(date) only, for a freshness check
--
-- Searched swing/, analysis/, ai/, tools/, control/, intraday/, execution/.
-- No historical reader exists.
--
-- SO WHAT IS ACTUALLY LOST
-- ------------------------
-- Not a field. Only the ability to RE-RUN compute_indicators for a day older
-- than the window — and even that is recoverable, because raw_prices comes from
-- the NSE bhavcopy and chartink_raw_data from the screener, both of which can
-- be fetched again. Nothing here is a fact that exists only in this database.
--
-- WHY 120 DAYS AND NOT THE 60 PROPOSED
-- ------------------------------------
-- 60 was chosen to be defensible; 120 is chosen to be comfortable. The live
-- path needs ONE day. 120 gives four months in which any night can still be
-- re-run from local data, costs about 0.42 MB per extra day across both tables,
-- and still removes the entire 13.6 MB/month growth that these two contribute —
-- which is the whole point. Buying four months of re-run headroom for ~25 MB of
-- steady state is the right side of that trade.
--
-- WHAT IS DELIBERATELY NOT TOUCHED
-- --------------------------------
--   price_history_yf   The operator's instinct was right and it is excluded.
--                      Unlike staging it is read with a 470-CALENDAR-DAY
--                      lookback (compute_indicators.py:207, covering ret_12m at
--                      240 sessions), so it is the one price table whose loss
--                      would break a computed field. It is also the slowest
--                      grower of the four at 2.55 MB/month. Most risk, least
--                      saving — left alone.
--
--   signal_output_daily, signal_log, intraday_setups, closed_positions
--                      The unbiased denominator. Under 10 MB, and the thing all
--                      the storage exists for. Never.
--
-- DELETE-ONLY, NO ARCHIVE HALF
-- ----------------------------
-- Migration 016 archives stock_data_daily before deleting because those rows
-- carry 86 computed columns that would be expensive to rebuild. Staging rows
-- carry nothing that is not already persisted downstream, so archiving them
-- would copy bytes from one table to another to no reader's benefit.
-- ═══════════════════════════════════════════════════════════════════════════


CREATE OR REPLACE FUNCTION public.rolloff_staging(keep_days integer DEFAULT 120)
RETURNS TABLE(table_name text, deleted bigint, cutoff date)
LANGUAGE plpgsql
AS $$
DECLARE cut date; n bigint;
BEGIN
  -- Calendar days, not trading days. Unlike the 200-day moving average that
  -- sets stock_data_daily's window, nothing here has a session-count
  -- requirement — the window exists to bound re-run headroom, and headroom is
  -- something a human measures in weeks.
  cut := (CURRENT_DATE - keep_days);

  DELETE FROM public.raw_prices WHERE date < cut;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT 'raw_prices'::text, n, cut;

  DELETE FROM public.chartink_raw_data WHERE date < cut;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT 'chartink_raw_data'::text, n, cut;
END $$;

COMMENT ON FUNCTION public.rolloff_staging IS
  'Delete ingestion staging older than keep_days. Safe because every field '
  'derived from these tables is already persisted in stock_data_daily, every '
  'reader queries a single date, and both sources are re-fetchable. Re-runnable '
  'and idempotent. Does NOT touch price_history_yf, which is read 470 days deep.';


INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('storage_staging_keep_days', '120',
   'Calendar days of raw_prices and chartink_raw_data kept. The live path reads '
   'one day; this window exists so any recent night can still be re-run from '
   'local data. Every field these tables feed is already persisted in '
   'stock_data_daily, and both sources are re-fetchable, so the only thing a '
   'shorter window costs is re-run convenience.',
   'Storage', 'run_pipeline.py', 'int', '120', 'CRITICAL'),

  ('storage_staging_rolloff_enabled', 'true',
   'Whether the evening pipeline prunes ingestion staging. These two tables are '
   '33% of the database and 45% of its growth while being read one day deep. '
   'Off reproduces pre-Phase-4 behaviour: unbounded growth.',
   'Storage', 'run_pipeline.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE raw_mb numeric; ck_mb numeric; old_raw bigint; old_ck bigint;
BEGIN
  SELECT round(pg_total_relation_size('public.raw_prices')/1048576.0, 1) INTO raw_mb;
  SELECT round(pg_total_relation_size('public.chartink_raw_data')/1048576.0, 1) INTO ck_mb;
  SELECT count(*) INTO old_raw FROM public.raw_prices        WHERE date < CURRENT_DATE - 120;
  SELECT count(*) INTO old_ck  FROM public.chartink_raw_data WHERE date < CURRENT_DATE - 120;

  RAISE NOTICE '';
  RAISE NOTICE 'Staging retention ready (120 days).';
  RAISE NOTICE '  raw_prices        % MB, % row(s) older than the window', raw_mb, old_raw;
  RAISE NOTICE '  chartink_raw_data % MB, % row(s) older than the window', ck_mb, old_ck;
  RAISE NOTICE '';
  RAISE NOTICE 'Nothing is deleted by this migration. The evening pipeline calls it.';
  RAISE NOTICE 'To prune now:  SELECT * FROM rolloff_staging(120);';
END $$;
