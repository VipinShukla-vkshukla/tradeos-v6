-- 094_sub_engine_on_positions.sql
-- 22-Aug-2026
--
-- `open_positions`/`closed_positions` never carried `sub_engine` —
-- `intraday_setups.meta.sub_engine` has correctly separated SDN's three
-- conditions since F-39/F-41 (VREJ 83% win rate, BRKD 8%, TRP 0% over the
-- resolved post-fix sample), but nothing forwarded that field past the
-- allocator's own paper-entry write. Every downstream reader — the
-- position-lifecycle tables, closed_positions, and the frontend's
-- StrategyBreakdown panel — was structurally unable to see it, no matter
-- how correct the underlying detection had become.
--
-- Additive only. NULL for every historical row (nothing to backfill from —
-- open_positions never had the value to begin with) and for every SWING
-- row (sub_engine is an intraday-only vocabulary). Readers fall back to
-- `strategy` when this is NULL, so nothing that reads these tables today
-- changes behaviour on old rows.
ALTER TABLE open_positions   ADD COLUMN IF NOT EXISTS sub_engine TEXT;
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS sub_engine TEXT;
