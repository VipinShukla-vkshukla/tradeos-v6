-- Migration 092: retention for intraday_quote_parity (unarmed by default)
--
-- WHY: intraday_quote_parity_log has been on since 04-Aug-2026 (never
-- disarmed) and the table hit 400k+ rows / ~45MB by 22-Aug — roughly
-- two-thirds of the month's 75MB storage growth (health check, 22-Aug).
--
-- Verified deepest reader before proposing a window: health.py's
-- check_quote_parity() — the only routine consumer — reads a 5-day cutoff
-- (tools/health.py:1071). tools.quote_parity's ad-hoc `report()` command
-- reads unbounded history for manual investigation, so the window below is
-- a generous multiple of the 5-day live need, not the 5 days itself.
--
-- Same shape as migration 032 (rolloff_staging): delete-only, no archive
-- half — this is diagnostic instrumentation, not the learning record.
-- Ships DISABLED, same as storage_staging_rolloff_enabled did, so this
-- migration changes nothing until the operator arms it.

CREATE OR REPLACE FUNCTION public.rolloff_quote_parity(keep_days integer DEFAULT 30)
RETURNS TABLE(deleted bigint, cutoff date)
LANGUAGE plpgsql
AS $$
DECLARE cut date; n bigint;
BEGIN
  cut := (CURRENT_DATE - keep_days);
  DELETE FROM public.intraday_quote_parity WHERE ts < cut;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n, cut;
END $$;

COMMENT ON FUNCTION public.rolloff_quote_parity IS
  'Delete quote-parity comparison rows older than keep_days. check_quote_parity() '
  'only reads the last 5 days; this window is a 6x margin for manual review, not '
  'the live floor. Re-runnable and idempotent.';


INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('storage_quote_parity_keep_days', '30',
   'Days of intraday_quote_parity kept. Live monitoring (check_quote_parity) '
   'reads 5 days; this is headroom for manual tools.quote_parity investigation.',
   'Storage', 'run_pipeline.py', 'int', '30', 'CRITICAL'),

  ('storage_quote_parity_rolloff_enabled', 'false',
   'Whether the evening pipeline prunes intraday_quote_parity. Off reproduces '
   'current behaviour: unbounded growth from a switch armed 04-Aug and never '
   'disarmed.',
   'Storage', 'run_pipeline.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE qp_mb numeric; old_qp bigint;
BEGIN
  SELECT round(pg_total_relation_size('public.intraday_quote_parity')/1048576.0, 1) INTO qp_mb;
  SELECT count(*) INTO old_qp FROM public.intraday_quote_parity WHERE ts < CURRENT_DATE - 30;
  RAISE NOTICE '';
  RAISE NOTICE 'Quote-parity retention ready, NOT ARMED.';
  RAISE NOTICE '  intraday_quote_parity  % MB, % row(s) older than 30 days', qp_mb, old_qp;
  RAISE NOTICE '  arm: UPDATE system_config SET value=''true'' WHERE key=''storage_quote_parity_rolloff_enabled'';';
END $$;
