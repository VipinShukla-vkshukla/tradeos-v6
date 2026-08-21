-- Migration 093: compact intraday_setups re-evaluation duplicates (unarmed)
--
-- WHY: measured live 22-Aug-2026 -- 15,845 raw rows collapse to 1,799
-- canonical (symbol, strategy, trade_date) groups, an 8.81x ratio. The
-- duplicates are re-evaluation ticks of one setup every 15s
-- (`_setup_is_new`, intraday/engine.py:3528) -- and every consumer already
-- collapses them to the same key before reading (`dedupe_setups()`,
-- tools/weekly_review.py:61, keeps the row with the EARLIEST ts, since "the
-- engine skips a symbol once it holds a position in it, and every later row
-- describes a chance that was already spent"). This function keeps exactly
-- that row and deletes the rest, so it cannot change any existing prior,
-- review, or discovery number -- they were never reading the deleted rows.
--
-- Restricted to outcome IS NOT NULL (resolved only, never a row resolve_day
-- might still need) and trade_date < keep_days (default 365 -- generous:
-- the live read is 90 days, this leaves a further 9 months for the kind of
-- ad-hoc historical digging this project does routinely). Ships DISABLED --
-- this is the learning table itself, not a diagnostic log, so it gets one
-- more degree of caution than migrations 032/092 did.
--
-- NOTE, not fixed here: dedupe_setups() keeps the EARLIEST row unconditionally,
-- even if a later row in the same group shows the cost verdict flipping to
-- TAKEN (_setup_is_new does allow that as a reason to write a new row). That
-- is a separate question about the existing scoring logic, not this
-- migration's storage question, and is left alone.

CREATE OR REPLACE FUNCTION public.compact_setups(keep_days integer DEFAULT 365)
RETURNS TABLE(deleted bigint, cutoff date)
LANGUAGE plpgsql
AS $$
DECLARE cut date; n bigint;
BEGIN
  cut := (CURRENT_DATE - keep_days);
  WITH keep AS (
    SELECT DISTINCT ON (symbol, strategy, trade_date) id
    FROM public.intraday_setups
    WHERE trade_date < cut AND outcome IS NOT NULL
    ORDER BY symbol, strategy, trade_date, ts ASC, id ASC
  )
  DELETE FROM public.intraday_setups s
  WHERE s.trade_date < cut AND s.outcome IS NOT NULL
    AND s.id NOT IN (SELECT id FROM keep);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n, cut;
END $$;

COMMENT ON FUNCTION public.compact_setups IS
  'Collapse resolved intraday_setups groups older than keep_days to the '
  'single earliest-ts row per (symbol, strategy, trade_date) -- the same row '
  'dedupe_setups() already treats as canonical. Re-runnable and idempotent.';


INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('storage_setups_compact_keep_days', '365',
   'Resolved intraday_setups rows older than this get compacted to one row '
   'per (symbol, strategy, trade_date). Live reads (priors, weekly review, '
   'discovery) go 90 days deep at most; this leaves 9 further months intact '
   'for manual historical review.',
   'Storage', 'run_pipeline.py', 'int', '365', 'CRITICAL'),

  ('storage_setups_compact_enabled', 'false',
   'Whether the evening pipeline compacts intraday_setups re-evaluation '
   'duplicates. This is the learning record itself, not a diagnostic log, so '
   'it stays off until reviewed and armed deliberately -- unlike the staging '
   'and quote-parity roll-offs, this is not a plain delete.',
   'Storage', 'run_pipeline.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE raw_n bigint; grp_n bigint;
BEGIN
  SELECT count(*) INTO raw_n FROM public.intraday_setups
    WHERE trade_date < CURRENT_DATE - 365 AND outcome IS NOT NULL;
  SELECT count(DISTINCT (symbol, strategy, trade_date)) INTO grp_n
    FROM public.intraday_setups WHERE trade_date < CURRENT_DATE - 365 AND outcome IS NOT NULL;
  RAISE NOTICE '';
  RAISE NOTICE 'Setups compaction ready, NOT ARMED.';
  RAISE NOTICE '  % resolved row(s) older than 365 days across % group(s)', raw_n, grp_n;
  RAISE NOTICE '  arm: UPDATE system_config SET value=''true'' WHERE key=''storage_setups_compact_enabled'';';
END $$;
