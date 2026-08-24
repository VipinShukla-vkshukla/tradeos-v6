-- 094_swing_stage_e3_ai_tighten_and_regime.sql
-- 24-Aug-2026 (Track E, Stage E3)
--
-- Two new switches, both SHIP OFF per Stage E3's own default posture
-- (docs/TRADEOS_ROADMAP.md): "changes live exit behavior on a book that
-- is currently working, which is a different risk profile from a
-- stall-clock number that can only ever tighten."
--
-- swing_ai_tighten_enabled: execute ai_recommended_action='TIGHTEN_SL' as
-- a real, one-directional stop adjustment. ai_decision_engine.py has
-- computed this recommendation all along; before this it was read by
-- exactly one place (alerts/send_alerts.py, to display it) and executed
-- by nothing. Lower risk than the regime switch below — it can only ever
-- protect capital, never spend it — but still ships inert until the
-- operator has watched the shadow log (a debug line per cycle when the
-- condition would fire) for real sessions first.
--
-- swing_regime_aware_exits_enabled: read the market's CURRENT regime
-- (not regime_at_entry, frozen the day a position opened) and adjust the
-- giveback/stall thresholds by it. E2's own quantify pass (F-68) found
-- every resolved swing outcome on record reads regime='NEUTRAL' — there
-- is no historical diversity yet to validate this against, which is
-- exactly why this one ships shadow-logged with no calibration behind it
-- at all, unlike F-43/F-46's ladder work.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('swing_ai_tighten_enabled', 'false',
   'Execute ai_recommended_action=TIGHTEN_SL as a real stop adjustment '
   '(one-directional, never loosens). Ships OFF; arm once the shadow log '
   'has been watched for real sessions. control/position_lifecycle.py '
   '::evaluate_exit, rung 2c.',
   'Exit ladder', 'control/position_lifecycle.py', 'bool', 'false', 'MEDIUM'),

  ('swing_ai_tighten_fraction', '0.5',
   'Fraction of the distance from the current stop to the live price the '
   'stop moves when swing_ai_tighten_enabled fires. 0.5 = halfway. Never '
   'produces a value below the current stop.',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '0.5', 'LOW'),

  ('swing_regime_aware_exits_enabled', 'false',
   'Adjust the giveback/stall thresholds by the market''s CURRENT regime '
   'rather than only the frozen regime_at_entry. Ships OFF — zero '
   'historical regime diversity exists yet to validate this against '
   '(F-68). control/position_lifecycle.py::evaluate_exit.',
   'Exit ladder', 'control/position_lifecycle.py', 'bool', 'false', 'MEDIUM'),

  ('swing_regime_mult_risk_off', '0.7',
   'Multiplier on giveback_pct/stall_days when the current regime reads '
   'RISK OFF and swing_regime_aware_exits_enabled is on. Below 1.0 = '
   'tighter (less giveback allowed, shorter stall clock).',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '0.7', 'LOW'),

  ('swing_regime_mult_risk_on', '1.2',
   'Multiplier on giveback_pct/stall_days when the current regime reads '
   'RISK ON or TRENDING and swing_regime_aware_exits_enabled is on. '
   'Above 1.0 = looser (more giveback allowed, longer stall clock) — '
   'more patience in a genuinely strong tape.',
   'Exit ladder', 'control/position_lifecycle.py', 'float', '1.2', 'LOW')
ON CONFLICT (key) DO NOTHING;
