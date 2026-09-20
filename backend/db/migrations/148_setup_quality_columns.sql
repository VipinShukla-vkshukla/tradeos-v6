-- 20-Sep-2026. setup_quality on signal_output_daily — a measurement, read by
-- nobody.
--
-- analysis/setup_quality.py scores each evening's plans on factors fitted only
-- on signals dated before 2026-08-16, kept only where the effect points the
-- same way in both halves of that window AND in the pooled window.
--
-- IT IS NOT A GATE, AND THIS IS THE EVIDENCE FOR NOT MAKING IT ONE. Tested as a
-- selection input through current mechanics — same candidate pool, same caps,
-- live sizing, real exit ladder, only the ORDER changed:
--
--   16-Aug..17-Sep holdout      n     win      sumR        net
--   production ranker          55    38.2%   -5.55R     -48,426
--   setup_quality              47    40.4%   -9.28R     -46,346
--
-- In-sample it read +7.89R against the ranker's +2.40R at 67.6% win. That gap
-- is the signature of a fitted score. The columns exist so the comparison can
-- be re-run later on data the score has never seen; the gate for reading it in
-- a decision is that it beats the production ranker out of sample, at the live
-- caps.
--
-- NULL is meaningful: fewer than 8 of the 11 factors present, or the scorer
-- itself unavailable. The writer never fails the pipeline over it.

ALTER TABLE public.signal_output_daily
    ADD COLUMN IF NOT EXISTS setup_quality            numeric,
    ADD COLUMN IF NOT EXISTS setup_quality_components jsonb;

COMMENT ON COLUMN public.signal_output_daily.setup_quality IS
    'Mean clipped z-score over analysis/setup_quality.FACTORS. Instrumentation only - nothing reads it for a trading decision (see migration 148 and the module docstring). NULL = fewer than 8 factors available.';
COMMENT ON COLUMN public.signal_output_daily.setup_quality_components IS
    'Per-factor z contributions, n_used and the missing factor names, so the weekly review can see WHICH component moved rather than only the total.';
