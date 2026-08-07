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
   'MEASURED 07-Aug-2026 (tools/hurdle_population_audit.py --framework '
   'INTRADAY, n=346 raw / 35 deduplicated, ~9.9x inflation): dedup moved the '
   'bar the OPPOSITE way from SWING -- p75 -1.329R -> -0.900R, p95 -0.110R '
   '-> -0.031R, both STRICTER, not looser. Cause: on intraday, the rows that '
   'repeat most are near-miss setups re-logged every 15s while they hover '
   'without triggering, which inflates the BAD tail of the raw population; '
   'dedup removes that and the bar rises. This is a structural property of '
   'how candidates are generated (which rows dwell longest), not a small-'
   'sample artifact, so accumulating more of the same kind of trades is not '
   'expected to flip it. VERDICT: do not enable. Intraday already runs on a '
   'thin edge net of MIS cost (see the cold-start landmine in CLAUDE.md) and '
   'a stricter bar makes that worse. Re-measure with this same tool only if '
   'the ENGINE MIX changes materially (new engine added/matured, or a change '
   'to how long marginal setups stay live before being dropped) -- not on a '
   'volume/time basis alone.',
   'Master controls', 'allocation/hurdle.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
