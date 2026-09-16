-- 17-Sep-2026. Swing market exposure: smaller and fewer entries in a correction.
--
-- analysis/market_exposure.py reads market_regime rows before the trading day.
-- CORRECTION = 2+ of: Nifty below its 50-session mean, Nifty 20-session change
-- <= -3%, median A/D over 5 sessions < 0.8. In CORRECTION the swing book takes
-- at most 2 new entries a day at half size.
--
-- Replay (tools.swing_fix_replay, trades actually taken since 14-Aug):
--   exposure alone vs fix 1:  net -9,127 -> -3,053, maxDD -10,633 -> -6,037
--   on top of the regime fix: net +1,068 -> +1,388
--   13-Jul..13-Aug trades:    unchanged (no entry fell on a CORRECTION day)
--
-- Premise check over 56 plan days (25-Jun..08-Sep): weakness signals came before
-- +0.135R per plan-day in the late-July pullback and -0.24R in the September
-- slide. Opposite signs, so only the risk-control half ships ARMED. Blocking
-- RISK OFF outright and the RS / no-chase selection filters ship OFF.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('swing_exposure_enabled', 'true',
   'Swing market-exposure state (analysis/market_exposure.py). false restores '
   'pre-17-Sep-2026 sizing and pacing exactly.',
   'Swing entries', 'analysis/market_exposure.py', 'bool', 'false', 'MEDIUM'),
  ('swing_correction_min_signals', '2',
   'Weakness signals (below 50-session mean, 20-session change, A/D median) '
   'needed for CORRECTION.',
   'Swing entries', 'analysis/market_exposure.py', 'int', '2', 'MEDIUM'),
  ('swing_correction_nifty_20d_pct', '-3.0',
   'Nifty 20-session change at or below this counts as a weakness signal.',
   'Swing entries', 'analysis/market_exposure.py', 'float', '-3.0', 'LOW'),
  ('swing_correction_ad_median', '0.8',
   'Median advance/decline ratio over 5 sessions below this counts as a '
   'weakness signal.',
   'Swing entries', 'analysis/market_exposure.py', 'float', '0.8', 'LOW'),
  ('swing_correction_max_new', '2',
   'Max new swing entries per day in CORRECTION (min with swing_max_new_per_day).',
   'Swing entries', 'analysis/market_exposure.py', 'int', '2', 'MEDIUM'),
  ('swing_correction_size_mult', '0.5',
   'Swing position size multiplier in CORRECTION (applied through decide() '
   'vol_mult, so the 3% minimum position still refuses a clip that falls under it).',
   'Swing entries', 'analysis/market_exposure.py', 'float', '0.5', 'MEDIUM'),
  ('swing_risk_off_block_entries', 'false',
   'Refuse every new swing entry while the swing regime is RISK OFF. OFF: '
   '24..30-Jul-2026 RISK OFF plans won 69-78%.',
   'Swing entries', 'analysis/market_exposure.py', 'bool', 'false', 'HIGH'),
  ('swing_correction_refuse_chase', 'false',
   'In CORRECTION, refuse plans priced above the zone or tagged CHASING/REENTRY. '
   'Unconfirmed, OFF.',
   'Swing entries', 'analysis/market_exposure.py', 'bool', 'false', 'MEDIUM'),
  ('swing_correction_rs_pctile', '0',
   'In CORRECTION, refuse plans below this percentile of the day''s RS vs Nifty '
   '(0 = off). Refused plans beat admitted ones in the only sample (n=16).',
   'Swing entries', 'analysis/market_exposure.py', 'float', '0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
