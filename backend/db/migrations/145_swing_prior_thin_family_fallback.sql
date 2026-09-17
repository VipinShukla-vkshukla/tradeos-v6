-- 18-Sep-2026. A swing family too thin AFTER swing_data_since keeps its own
-- measured history instead of scoring as if it had never been measured.
--
-- With swing_data_since = 2026-09-01, RVS kept 16 of its 31 plans — under the
-- priors_min_sample_swing floor of 30 — so swing_priors() gave it the neutral
-- 0 prior and it ranked FIRST of the three families:
--
--   family         prior mean R        edge/day (cost 0.0994R, hold 3.5d)
--   RVS            +0.000 (n=13*)      -0.0284   <- ranked first
--   CONTINUATION   -0.412 (n=150)      -0.1460
--   MOM            -0.420 (n=45)       -0.1483
--
-- RVS is the worst family on record: -0.451R over 50 plans. CLAUDE.md's own
-- landmine — "No opinion" and "measured bad" must not give the same answer.
-- With this on, RVS reads -0.451 (n=50) and ranks last; CONTINUATION and MOM
-- are untouched. A family with genuinely NO history keeps the neutral prior,
-- so a new engine still starts from no opinion rather than inheriting someone
-- else's record. Self-expiring: once RVS has 30 resolved plans since the
-- cutoff (16 now, ~2 a session) the fallback stops firing on it.
--
-- No verdict changes today: the hurdle bar sits at ~+0.016/day and every edge
-- is negative either way, and paper entries do not consult the allocator while
-- swing_paper_research_mode is on. This decides slot priority when swing goes
-- LIVE, and what the Sunday brain reads as the best family.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('swing_prior_thin_family_fallback', 'true',
   'When a swing family has fewer than priors_min_sample_swing resolved plans '
   'after swing_data_since, use its full-history prior instead of the neutral '
   'one. A family with no history at all stays neutral.',
   'Swing learning', 'allocation/scoring.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
