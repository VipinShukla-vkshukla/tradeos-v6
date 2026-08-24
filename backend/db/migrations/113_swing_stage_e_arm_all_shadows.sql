-- 113_swing_stage_e_arm_all_shadows.sql
-- 24-Aug-2026 (Track E, arming pass)
--
-- Operator's own explicit instruction, given full awareness of the
-- evidence split: "arm all the shadow Es to live mode." Arms all seven
-- Track E shadow switches built this session (E3-E5) in one pass. Two
-- materially different evidence tiers, both armed on the operator's
-- explicit call, not a default this project assumes going forward —
-- the same one-time exception precedent as F-43/F-46's own straight-
-- to-live shipping (docs/TRADEOS_ROADMAP.md's own non-negotiables
-- section names that as "the operator's own explicit, stated call, not
-- a default this track assumes going forward").
--
-- SEASONED (fired repeatedly against real open positions this session):
--   swing_ai_tighten_enabled        — HINDCOPPER, multiple real fires
--   swing_regime_aware_exits_enabled
--   swing_sector_decay_enabled      — HINDCOPPER/AARTIIND, multiple fires
--
-- ZERO OR NEAR-ZERO REAL FIRINGS (built this session; thresholds are
-- explicitly documented as starting points, not calibrated bars):
--   swing_early_invalidation_enabled   — never fired on real data
--   swing_participation_decay_enabled  — never fired on real data
--   entry_refuse_low_rr_retention      — fired once (GLAND)
--   entry_refuse_broken_trend          — never fired on real data
--
-- Flagged to the operator before this migration was written; armed on
-- their explicit confirmation to proceed with all seven regardless.
UPDATE system_config SET value = 'true' WHERE key IN (
  'swing_ai_tighten_enabled',
  'swing_regime_aware_exits_enabled',
  'swing_sector_decay_enabled',
  'swing_early_invalidation_enabled',
  'swing_participation_decay_enabled',
  'entry_refuse_low_rr_retention',
  'entry_refuse_broken_trend'
);
