-- 111_swing_sector_decay_strength_exemption.sql
-- 24-Aug-2026 (Track E, Stage E4 refinement)
--
-- Operator's own point: the sector-decay multiplier (migration 095) should
-- not punish a position for its sector's group-level WEAKENING read when
-- the position's OWN volume is holding or rising — a genuine leader can
-- outrun a lagging group ("buy the strongest stock in a weak sector" —
-- O'Neil/Minervini both make this point). Reuses the participation-decay
-- ratio (migration 110): when a WEAKENING-sector position's own vol_ratio
-- is at or above this floor (i.e. participation has NOT decayed vs. its
-- entry day), the sector-decay tighten is skipped for that position.
-- Deliberately asymmetric — only the sector (group-level) multiplier
-- defers to stock-level strength; the regime multiplier is unaffected,
-- since a genuine risk-off regime is systemic and not something one
-- stock's own volume can diversify away from.
--
-- No new switch: governed by the same swing_sector_decay_enabled this
-- refines. Ships with a sensible default (1.0 = participation at least
-- at entry-day level counts as "not decayed").
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('swing_sector_decay_strength_exempt_floor', '1.0',
   'A WEAKENING-sector position whose own vol_ratio(latest)/vol_ratio'
   '(entry-day) is at or above this floor is exempted from the '
   'swing_sector_decay_mult tighten — demonstrated individual strength '
   'overrides a group-level sector read. Only applies when '
   'swing_sector_decay_enabled is on (or shadow-logged when off).',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '1.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
