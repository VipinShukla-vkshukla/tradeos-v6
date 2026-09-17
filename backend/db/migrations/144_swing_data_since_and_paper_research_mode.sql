-- 17-Sep-2026. Two operator decisions for the paper swing book.
--
-- 1. swing_data_since = 2026-09-01. The operator treats the swing framework as
--    it now runs as starting on 01-Sep. Every reader that LEARNS from swing
--    history (allocator swing priors and hold-day estimates, the weekly brain's
--    performance tracker, weekly_review, expectancy_ledger, the feature-edge and
--    family-maturity audits, tools.swing_fix_replay) filters to this date.
--    Nothing is deleted: 108 pre-Sep swing closes (91 real money) stay in
--    closed_positions for reconcile, tax and the dashboard. One documented
--    exception: the stall-clock pace calibration keeps full history — from 01-Sep
--    only it would cut CONTINUATION's clock from 8 to 3 sessions on one tape.
--
-- 2. swing_paper_research_mode = true. From 09-Sep the allocator declined every
--    swing proposal ("edge below the bar") and the paper book took no trades.
--    While swing is PAPER, allocator and market-exposure verdicts are recorded
--    (allocation_decisions, logs) but do not block entries, so the book collects
--    trade data. execution.gates.swing_research_mode() is False whenever swing
--    is LIVE regardless of this key; tools.health `research_mode` fails if it is
--    left on while LIVE. Decision gates that describe trade QUALITY stay on:
--    decide() stop/target/R:R, entry refusals, liquidity, one book per symbol,
--    the daily cap and slot limits.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('swing_data_since', '2026-09-01',
   'ISO date before which swing history is excluded from learning and analysis '
   '(config.swing_data_since). Empty = all history. Nothing is deleted.',
   'Swing learning', 'config.py', 'str', '', 'MEDIUM'),
  ('swing_paper_research_mode', 'true',
   'PAPER swing only: allocator and market-exposure verdicts recorded, not '
   'enforced, so the paper book collects trade data. Ignored when swing is LIVE.',
   'Swing entries', 'execution/gates.py', 'bool', 'false', 'HIGH')
ON CONFLICT (key) DO NOTHING;
