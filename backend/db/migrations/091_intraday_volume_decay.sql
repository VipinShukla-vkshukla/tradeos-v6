-- 091_intraday_volume_decay.sql
-- 20-Aug-2026
--
-- Rung 7a of evaluate_intraday_exit(): a LEADING signal ahead of the fixed
-- 75-minute time stop — is the follow-through volume a trade opened on
-- still there, or has it faded. Ships correctly wired and OFF by default,
-- same posture as intraday_giveback_pct did before migration 059 armed it:
-- this exact question has never been measured against this book's own
-- resolved trades. Arm once tools.exit_ladder_replay-style evidence exists
-- for this specific signal.
--
-- See intraday/exit_policy.py::_volume_decay_ratio and rung 7a in
-- evaluate_intraday_exit for the mechanics.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_volume_decay_enabled', 'false',
   'Rung 7a of the intraday exit ladder: tighten the stop when follow-'
   'through volume has faded well below what the trade opened on, ahead '
   'of the fixed-clock time stop noticing anything. OFF by default -- a '
   'plausible, professionally-grounded hypothesis (a discretionary '
   'trader watches exactly this) with zero hours of calibration against '
   'this book''s own resolved trades. Arm once exit_ladder_replay-style '
   'evidence exists for this specific signal, same arc as '
   'intraday_giveback_pct (migration 059).',
   'Master controls', 'intraday/exit_policy.py', 'bool', 'false', 'MEDIUM'),

  ('intraday_volume_decay_window_min', '15',
   'Minutes per window for the volume-decay comparison -- the first N '
   'minutes after entry (the trade''s own opening pace) against the last '
   'N minutes up to now. Below 2*this many minutes held, the rung has '
   'nothing to compare yet and stays silent regardless of the switch.',
   'Master controls', 'intraday/exit_policy.py', 'int', '15', 'LOW'),

  ('intraday_volume_decay_floor_pct', '40.0',
   'Recent-vs-initial volume pace below this fraction (40% = recent pace '
   'is under 40% of what the trade opened on) is "decaying" and, when '
   'intraday_volume_decay_enabled is armed, tightens the stop. Untuned -- '
   'a starting number, not a calibrated one.',
   'Master controls', 'intraday/exit_policy.py', 'float', '40.0', 'MEDIUM'),

  ('intraday_volume_decay_tighten_pct', '50.0',
   'How far to tighten when volume decay fires -- the new stop sits this '
   'fraction of the ORIGINAL risk width from the live price, never '
   'looser than whatever the stop already is. Same linear-toward-price '
   'shape as intraday_short_runway_tighten_floor_pct (rung 6b), reused '
   'rather than a third tightening formula in one file.',
   'Master controls', 'intraday/exit_policy.py', 'float', '50.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
