-- 24-Sep-2026. IGN trend logic beyond the long gate, ALL DISARMED: the short-side gate,
-- the confidence-score weight, and the forming-candle switch (intraday/ign_trend.py).
--
-- Same reason as migration 149: the daily-trend study (docs/FINDINGS.md, 24-Sep-2026,
-- "IGN daily-trend study RESULT") found no signal that separates IGN's entries, so
-- nothing here may change a trade until a pre-registered study says so. With every
-- value below, IGN's setups are byte-identical to before. The score and the
-- forming-candle panel are RECORDED on every detection regardless (Setup.meta['trend'],
-- ['trend_live']) so evidence accrues.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('ign_trend_confidence_weight', '0',
   'How far IGN''s trend score (-1..+1, five fixed pre-registered checks, mirrored for '
   'shorts) moves its confidence: confidence += weight * score, kept in [0.05, 0.85]. 0 = off. '
   'Confidence is what alloc_intraday_confidence_bands classifies a proposal by, so this is '
   'the one place a validated daily signal would reach the allocator. No signal is validated.',
   'Intraday engines', 'intraday/ign_trend.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_use_forming', 'false',
   'When true the trend gates and the score read the panel recomputed with TODAY''s forming '
   'daily candle instead of the as-of-prior-close panel. A different, unvalidated quantity '
   '(a spike sits above its own SuperTrend by construction). If it cannot be built the gate '
   'abstains rather than falling back to the other panel.',
   'Intraday engines', 'intraday/ign_trend.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_short_gate_enabled', 'false',
   'IGN daily trend gate for SHORT setups, the mirror of ign_trend_gate_enabled: price below '
   'the SuperTrend, -DI at or above a floor, SMA50 below SMA200. Independent of the long '
   'gate. DISARMED: no short-side evidence exists yet.',
   'Intraday engines', 'intraday/ign_trend.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_short_require_below_st', 'false',
   'Short gate check: require the close BELOW the daily SuperTrend(10,3). Off = not part of the gate.',
   'Intraday engines', 'intraday/ign_trend.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_short_min_adx', '0',
   'Short gate check: require Wilder ADX(14) >= this. 0 = off.',
   'Intraday engines', 'intraday/ign_trend.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_short_min_di_minus', '0',
   'Short gate check: require Wilder -DI(14) >= this (selling pressure). 0 = off.',
   'Intraday engines', 'intraday/ign_trend.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_short_max_prev_vol_ratio', '0',
   'Short gate check: require the last completed session''s volume / mean of the prior 20 <= this. 0 = off.',
   'Intraday engines', 'intraday/ign_trend.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_short_require_sma50_lt_200', 'false',
   'Short gate check: require SMA50 < SMA200 from Kite daily closes. Off = not part of the gate.',
   'Intraday engines', 'intraday/ign_trend.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_short_min_agree', '0',
   'How many ENABLED short-gate checks must pass. 0 = all of them; capped at the number that had data.',
   'Intraday engines', 'intraday/ign_trend.py', 'int', '0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
