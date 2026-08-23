-- 104_stage_d2h_admitted_universe_readiness.sql
-- 24-Aug-2026 (Stage D2h, docs/TRADEOS_ROADMAP.md Track D)
--
-- F-61: three real gaps found while auditing whether the ~270-name
-- Track D universe is actually SAFE to trade from once both live-
-- requalify switches are armed. All three are config-driven; the two
-- below are new keys. (The third fix, prior segmentation, adds no new
-- config -- it reuses meta.universe_population, already stamped.)
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('cost_slippage_thin_threshold_cr', '25.0',
   'Stage D2h: a name whose own daily traded value (value_cr) is below '
   'this is priced with wider paper slippage (cost_slippage_thin_'
   'multiplier) instead of the flat cost_slippage_bps figure. 25cr matches '
   'intraday_min_turnover_cr''s own default -- the same liquidity question, '
   'reused rather than a second number invented for it.',
   'Master controls', 'execution/paper_broker.py', 'float', '25.0', 'LOW'),

  ('cost_slippage_thin_multiplier', '3.0',
   'Stage D2h: multiplies cost_slippage_bps for a name below cost_'
   'slippage_thin_threshold_cr. Population B/C names are structurally the '
   'thinnest in the whole universe -- without this their paper fills were '
   'modelled with the same execution quality as a Nifty-50 name, making '
   'their paper track record systematically more optimistic than a real '
   'fill in them would be.',
   'Master controls', 'execution/paper_broker.py', 'float', '3.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
