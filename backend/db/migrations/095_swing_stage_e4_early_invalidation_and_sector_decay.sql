-- 095_swing_stage_e4_early_invalidation_and_sector_decay.sql
-- 24-Aug-2026 (Track E, Stage E4)
--
-- Both switches ship OFF, per Stage E4's own default posture — a
-- materially bigger behavioural change than Stage E3's rungs, since both
-- can close a position (or shorten its runway) the ordinary stop and
-- calibrated clocks would have left alone longer.
--
-- swing_early_invalidation_enabled: run the SAME structural-break check
-- (assess_trend -> deterioration_check) that already protects a
-- profitable position (gain_r >= 1.0) at ANY gain_r below that floor,
-- labelled EXIT_INVALIDATED rather than EXIT_DETERIORATION. Cannot fire
-- from a manufactured reading of a losing position by itself -- tq.
-- verdict must independently read BROKEN, the same bar the profitable
-- case already trusts, and stop-breach is checked first in the ladder so
-- this can only ever act on a position still above its own stop.
--
-- swing_sector_decay_enabled: read the position's CURRENT sector_state
-- (sector_strength, already computed every session) rather than only
-- sector_rank_at_entry, checked in exactly one place today (the 3R
-- runner decision) using the frozen entry-day snapshot. Tighten-only —
-- unlike the regime multiplier (Stage E3), a sector that is LEADING does
-- not earn extra patience on top of whatever the regime already grants.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('swing_early_invalidation_enabled', 'false',
   'Run the structural-break check (assess_trend -> deterioration_check) '
   'at any gain_r, not just gain_r >= exit_deterioration_min_r. Labelled '
   'EXIT_INVALIDATED, distinct from EXIT_DETERIORATION. Ships OFF; arm '
   'once the shadow log has been watched for real sessions. control/'
   'position_lifecycle.py::evaluate_exit, rung 2b2.',
   'Exit ladder', 'control/position_lifecycle.py', 'bool', 'false', 'HIGH'),

  ('swing_sector_decay_enabled', 'false',
   'Tighten the giveback/stall thresholds when the position''s CURRENT '
   'sector_strength.sector_state reads WEAKENING, rather than only '
   'checking sector_rank_at_entry (the frozen entry-day snapshot) at the '
   '3R runner decision. Tighten-only. Ships OFF. control/'
   'position_lifecycle.py::evaluate_exit.',
   'Exit ladder', 'control/position_lifecycle.py', 'bool', 'false', 'MEDIUM'),

  ('swing_sector_decay_mult', '0.75',
   'Multiplier on giveback_pct/stall_days when the position''s sector '
   'reads WEAKENING and swing_sector_decay_enabled is on. Below 1.0 = '
   'tighter. Composes multiplicatively with the Stage E3 regime '
   'multiplier (both apply if both are armed).',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '0.75', 'LOW')
ON CONFLICT (key) DO NOTHING;
