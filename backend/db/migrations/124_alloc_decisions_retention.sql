-- Migration 124: retention for allocation_decisions (unarmed by default)
--
-- 27-Aug-2026. Same storage-crisis investigation as migrations 092/032, one
-- table later: allocation_decisions had zero retention at all, is the
-- single biggest table in the database, and is growing fastest -- 95% of
-- its recent write volume comes from SWING candidates re-logged every
-- 15s cycle while they sit near their zone (see docs/FINDINGS.md,
-- 27-Aug-2026).
--
-- Unlike raw_prices/chartink_raw_data (migration 032), these rows are NOT
-- re-derivable -- each is a record of what the allocator decided using
-- priors/regime context that no longer exists to recompute. So this is
-- archive-then-delete, not delete-only, and because the archive target is
-- a local file rather than another Postgres table, the archive step is
-- application-level Python (tools/archive_allocation_decisions.py), not a
-- single PL/pgSQL function -- it exports and verifies a round-trip BEFORE
-- deleting anything, same safety contract, different mechanism.
--
-- alloc_hurdle_lookback_days moves from 90 to 60 alongside this: the live
-- window and the physically-retained window must stay the same number, or
-- hurdle() silently reads less history than its own config claims to want.

UPDATE public.system_config
SET value = '60'
WHERE key = 'alloc_hurdle_lookback_days';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('storage_alloc_decisions_keep_days', '60',
   'Days of allocation_decisions kept live. Matches alloc_hurdle_lookback_days '
   '-- the two must move together, since hurdle() reads a rolling window of '
   'exactly this length and a mismatch means it silently sees less history '
   'than its own config claims. Rows older than this are exported to '
   'db/archive/ (Parquet, verified round-trip) then deleted, never the '
   'reverse order.',
   'Storage', 'tools/archive_allocation_decisions.py', 'int', '60', 'CRITICAL'),

  ('storage_alloc_decisions_rolloff_enabled', 'false',
   'Whether the evening pipeline runs the allocation_decisions archive job. '
   'Off reproduces current behaviour: unbounded growth. Ships false, same '
   'posture as storage_quote_parity_rolloff_enabled did before its own '
   'measurement -- arm only after confirming the export+verify path works '
   'against real data.',
   'Storage', 'run_pipeline.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;
