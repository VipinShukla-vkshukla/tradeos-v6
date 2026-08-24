-- 110_swing_stage_e4_participation_decay.sql
-- 24-Aug-2026 (Track E, Stage E4)
--
-- Numbered 110, not the next sequential slot after 095, because the
-- concurrent intraday-track session had already claimed 096-109 on disk
-- (096_sdn_breakdown_retest.sql etc.) by the time this was written — the
-- shared migration ledger CLAUDE.md warns about. Applied after the
-- highest number in use to avoid a second collision, not because 96
-- swing migrations preceded it.
--
-- Ships OFF, per Stage E4's own default posture.
--
-- swing_participation_decay_enabled: the swing-cadence version of the
-- intraday F-45 volume-decay idea. vol_ratio on the entry-day session
-- (stock_data_daily) vs. the latest available session, per held SWING
-- symbol, fetched once per policy load by control/position_lifecycle.py
-- ::load_live_exit_context and read by evaluate_exit(). When the ratio
-- drops below swing_participation_decay_threshold AND the position has
-- been held at least 2 sessions (never flags on entry day or day one),
-- applies swing_participation_decay_mult to the giveback/stall
-- thresholds. Tighten-only, composes multiplicatively with the Stage E3
-- regime multiplier and the Stage E4 sector-decay multiplier already in
-- the chain — a stall clock counts SESSIONS, not conviction, and a name
-- stalling on thinning volume is a different animal from one stalling on
-- thick, contested volume.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('swing_participation_decay_enabled', 'false',
   'Tighten the giveback/stall thresholds when a held SWING position''s '
   'vol_ratio has decayed below swing_participation_decay_threshold of '
   'its entry-day value (min 2 sessions held). Tighten-only. Ships OFF; '
   'arm once the shadow log has been watched for real sessions. control/'
   'position_lifecycle.py::evaluate_exit.',
   'Exit ladder', 'control/position_lifecycle.py', 'bool', 'false', 'MEDIUM'),

  ('swing_participation_decay_threshold', '0.5',
   'vol_ratio(latest) / vol_ratio(entry-day) below this triggers the '
   'participation-decay multiplier when swing_participation_decay_enabled '
   'is on. Lower = more tolerant of thinning volume before tightening.',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '0.5', 'LOW'),

  ('swing_participation_decay_mult', '0.75',
   'Multiplier on giveback_pct/stall_days when a position''s participation '
   'has decayed past swing_participation_decay_threshold and '
   'swing_participation_decay_enabled is on. Below 1.0 = tighter. Composes '
   'multiplicatively with the Stage E3 regime multiplier and the Stage E4 '
   'sector-decay multiplier (all apply if all are armed).',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '0.75', 'LOW')
ON CONFLICT (key) DO NOTHING;
