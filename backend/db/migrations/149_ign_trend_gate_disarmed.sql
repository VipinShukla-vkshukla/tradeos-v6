-- 24-Sep-2026. IGN daily trend-health gate: the machinery, DISARMED, plus the
-- Kite daily-history feed it reads.
--
-- WHY IT SHIPS DISARMED. The evidence that motivated it was pseudo-replicated:
-- the "n=3,824" IGN outcome rows were 54 independent symbol-days over 10
-- trading days (35 usable LONG), each re-recorded ~73 times. Redone at that
-- unit the five candidate signals were not distinguishable from noise, and two
-- changed sign (sma50_gt_200, delivery_pct). Nothing here changes a trade.
-- What it does change: every IGN detection now carries the Kite-sourced feature
-- panel in intraday_setups.meta->'trend', so independent evidence accrues
-- prospectively, and tools/replay/ign_feature_study.py can replay IGN over a far
-- larger history to decide what, if anything, to arm (docs/FINDINGS.md,
-- 24-Sep-2026).
--
-- ARMING IS A SEPARATE, LATER MIGRATION and only for signals that pass the
-- study's pre-registered holdout criteria. With every threshold at its "off"
-- value below, ign_trend_gate_enabled=true would still refuse nothing.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('daily_history_enabled', 'true',
   'Kite daily-bar history for the IGN trend panel, 24-Sep-2026. One '
   'historical_data call per watched symbol per trading day on a background '
   'thread (never the 15s loop) plus one ltp call per 200 symbols for tokens. '
   'Read-only: false stops the fetching and every context carries daily_feats=None, '
   'which every consumer treats as "abstain". See intraday/daily_history.py.',
   'Intraday engines', 'intraday/daily_history.py', 'bool', 'true', 'LOW'),
  ('daily_history_spacing_s', '0.7',
   'Seconds between daily-history historical_data calls. Kite allows ~3 requests/s '
   'and refresh_contexts() skips a symbol''s bars for a whole cycle when ITS call is '
   'rate limited, so this thread leaves headroom rather than racing it.',
   'Intraday engines', 'intraday/daily_history.py', 'float', '0.7', 'LOW'),
  ('daily_history_lookback_days', '420',
   'Calendar days of daily candles fetched per symbol (~290 sessions: SMA-200 plus '
   'Wilder/SuperTrend warm-up).',
   'Intraday engines', 'intraday/daily_history.py', 'int', '420', 'LOW'),
  ('ign_trend_gate_enabled', 'false',
   'IGN daily trend-health gate master switch, 24-Sep-2026. LONG setups only. '
   'DISARMED: the evidence behind the candidate signals did not survive being '
   'redone at the independent symbol-day unit (n=35). Arm only the signals '
   'tools/replay/ign_feature_study.py confirms on its frozen holdout. When false '
   'IGN only RECORDS the panel in Setup.meta[''trend''].',
   'Intraday engines', 'intraday/strategies/ignition.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_require_above_st', 'false',
   'Gate check: require close above the Kite-computed daily SuperTrend(10,3) as of '
   'the last completed session. Off = not part of the gate.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_min_adx', '0',
   'Gate check: require Wilder ADX(14) >= this. 0 = off.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_max_di_minus', '0',
   'Gate check: require Wilder -DI(14) <= this. 0 = off.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_max_prev_vol_ratio', '0',
   'Gate check: require the last completed session''s volume / mean of the prior 20 '
   'sessions <= this. 0 = off. (Yesterday''s ratio, not today''s session volume, which '
   'IGN already gates on via ign_min_volume_ratio.)',
   'Intraday engines', 'intraday/strategies/ignition.py', 'float', '0', 'MEDIUM'),
  ('ign_trend_require_sma50_gt_200', 'false',
   'Gate check: require SMA50 > SMA200 computed from Kite daily closes. Off = not '
   'part of the gate.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'bool', 'false', 'MEDIUM'),
  ('ign_trend_min_agree', '0',
   'How many ENABLED gate checks must pass. 0 = all of them. Capped at the number '
   'of checks that had data, so one missing field does not make a majority rule '
   'impossible. A symbol with no panel, or one that failed its integrity check, '
   'abstains (allowed) rather than being refused.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'int', '0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
