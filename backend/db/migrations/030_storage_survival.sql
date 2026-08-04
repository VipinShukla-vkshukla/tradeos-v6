-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 030: switches for the storage guard (Phase 4, Stage 1)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Migration 016 built the survival machinery — a slim archive table, an
-- archive-then-delete roll-off function, and a usage view — and then left all
-- three sitting unused. Measured on 04-Aug-2026:
--
--     archive_stock_data()   never called from any Python file, workflow,
--                            launcher subcommand or scheduled job
--     v_storage_usage        never read by anything
--
-- So the database has been growing unwatched for five months. This migration
-- adds nothing to the schema. It only creates the four settings that let the
-- existing machinery be switched on, tuned, and rolled back without a deploy.
--
-- WHAT WAS ACTUALLY MEASURED, 04-Aug-2026
-- ---------------------------------------
--   total                205.6 MB   41.1% of the 500 MB plan ceiling
--   growth                30 MB/month, measured from rows added in 30 days
--   80% (health FAIL)    14-Feb-2027
--   100% (writes fail)   25-May-2027
--
-- Migration 016's own header estimated stock_data_daily at "roughly 190 MB"
-- across 48,967 rows. It is 58 MB across 53,463 — the estimate was high by a
-- factor of three. That is why nothing here is estimated: the health check
-- derives growth from rows actually added, and both thresholds are settings so
-- the guard can be shown failing rather than assumed working.
--
-- WHY keep_days = 250 IS A NO-OP TODAY, AND THAT IS CORRECT
-- --------------------------------------------------------
-- stock_data_daily holds 99 distinct trading dates. archive_stock_data(250)
-- therefore finds no cutoff and returns (0, 0, NULL) without touching a row.
-- Scheduling it now is not premature — it means the day the window is finally
-- reached, roll-off is already a habit rather than a thing someone remembers
-- during an incident. 250 trading days is set by the 200-day moving average,
-- the longest lookback anything in this system actually reads.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('storage_rolloff_enabled', 'true',
   'Whether the evening pipeline runs archive_stock_data() as its final step. '
   'Off reproduces pre-Phase-4 behaviour exactly: the function exists and is '
   'never called. Restored to false by tools/rollback --all-off.',
   'Storage', 'run_pipeline.py', 'bool', 'false', 'CRITICAL'),

  ('storage_rolloff_keep_days', '250',
   'Trading days of full 86-column detail kept in stock_data_daily. Anything '
   'older is copied to stock_data_archive (9 columns) and then deleted, in '
   'that order and in one transaction. 250 is set by the 200-day moving '
   'average — the longest lookback any consumer reads. Lowering this deletes '
   'history that indicator computation still needs.',
   'Storage', 'run_pipeline.py', 'int', '250', 'CRITICAL'),

  ('storage_ceiling_mb', '500',
   'The plan''s write ceiling. Past it Postgres refuses writes, which means '
   'the evening pipeline produces no signals. Raise this only when the plan '
   'itself is raised — an optimistic number here silences the one guard that '
   'sees the wall coming.',
   'Storage', 'tools/health.py', 'float', '500', 'CRITICAL'),

  ('storage_fail_pct', '80',
   'Percentage of storage_ceiling_mb at which the health check FAILS — not '
   'warns. 80 rather than 100 because a migration run against a near-full '
   'database can itself fail, so the alarm has to sound while there is still '
   'room to act on it. Lower this temporarily to prove the guard fires.',
   'Storage', 'tools/health.py', 'float', '80', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE total_mb numeric;
BEGIN
  SELECT round(sum(pg_total_relation_size(c.oid)) / 1024.0 / 1024.0, 1)
    INTO total_mb
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind = 'r';

  RAISE NOTICE '';
  RAISE NOTICE 'Storage guard wired. Database is % MB of 500 MB.', total_mb;
  RAISE NOTICE '  health now FAILS above storage_fail_pct:';
  RAISE NOTICE '      cd backend && python -m tools.health';
  RAISE NOTICE '  prove it can fail before trusting it green:';
  RAISE NOTICE '      UPDATE system_config SET value=''20'' WHERE key=''storage_fail_pct'';';
  RAISE NOTICE '  roll-off now runs as the final evening step (no-op until 250 trading days).';
  RAISE NOTICE '  nothing was deleted or dropped by this migration.';
END $$;
