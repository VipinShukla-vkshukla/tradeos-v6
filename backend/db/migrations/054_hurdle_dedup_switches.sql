-- Two new switches, both OFF by default: allocation/hurdle.py::_empirical_base()
-- builds its percentile population from every row in allocation_decisions --
-- one per (proposal, 15-second cycle), no dedup by symbol or day. A candidate
-- that sits near its entry zone for hours contributes hundreds of near-identical
-- edge values to the very population its own bar is compared against.
--
-- Confirmed live, SWING, 07-Aug-2026: ten different symbols each logged
-- 548-600 rows in a single session -- widespread repetition across many
-- names, not a couple of outliers. Whether deduplicating actually moves the
-- bar enough to matter is a measurement question (tools/hurdle_population_
-- audit.py), not something to assume -- these switches let SWING and
-- INTRADAY be decided independently and only after that measurement, per
-- the operator's explicit requirement that intraday's live behaviour not
-- change while swing is evaluated on its own.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('alloc_hurdle_dedup_swing', 'false',
   'When true, allocation/hurdle.py::_empirical_base() collapses SWING''s '
   'allocation_decisions population to one mean-edge observation per '
   '(symbol, trade_date) before taking the percentile bar, instead of one '
   'row per 15-second poll. OFF by default -- run '
   'tools/hurdle_population_audit.py --framework SWING first and confirm '
   'the p75/p95 delta is real before enabling. Independent of '
   'alloc_hurdle_dedup_intraday; enabling one never affects the other book.',
   'Master controls', 'allocation/hurdle.py', 'bool', 'false', 'MEDIUM'),
  ('alloc_hurdle_dedup_intraday', 'false',
   'Same mechanism as alloc_hurdle_dedup_swing, scoped to INTRADAY only. '
   'Intraday setups are minutes long rather than hours, so the same '
   'repetition-inflation is expected to be smaller there -- but expected is '
   'not measured. Keep this off (intraday''s live behaviour unchanged) '
   'until tools/hurdle_population_audit.py --framework INTRADAY has been '
   'run and reviewed on its own evidence, not inherited from swing''s.',
   'Master controls', 'allocation/hurdle.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
