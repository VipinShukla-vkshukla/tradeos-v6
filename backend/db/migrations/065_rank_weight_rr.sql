-- 11-Aug-2026. `rank_weight_rr` was read via cfg_float("rank_weight_rr", 1.0)
-- in analysis/entry_ranking.py while existing in NO migration and NO
-- system_config row -- the single largest-magnitude component left in
-- score_plan() once migration 060 rescaled final_score down, running
-- entirely on a silent code default nothing could see or audit.
--
-- allocation.scoring.rr_tercile_report() ran the same tercile methodology
-- migration 060 used for final_score, on the exact fallback chain
-- score_plan() reads (implied_rr, else expected_r), against
-- signal_output_daily's resolved CONTINUATION plans (n=188):
--
--   LOW rr tercile:  mean R +0.561 (n=63)
--   MID rr tercile:  mean R +0.439 (n=79)
--   HIGH rr tercile: mean R +0.448 (n=46)
--
-- No monotonic positive separation -- if anything LOW outperformed HIGH,
-- the opposite of what full weight on this term assumes. See
-- KNOWLEDGE_BASE.md and analysis/entry_ranking.py's own comment at this
-- term for the full reasoning and confidence caveat (medium -- the gap is
-- within roughly one combined standard error, so this is evidence AGAINST
-- the assumed positive relationship, not proof of a true inverse one).
--
-- Reduced, not zeroed: unlike final_score's opaque composite, reward:risk
-- has first-principles grounding this measurement did not disprove, only
-- failed to confirm at this sample size.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('rank_weight_rr', '0.4',
   'Weight of the reward:risk term (implied_rr, falling back to expected_r) '
   'in entry ranking. Reduced from the prior silent default of 1.0 on '
   'allocation.scoring.rr_tercile_report()''s finding: n=188 resolved '
   'CONTINUATION plans show no monotonic positive relationship between '
   'this term and forward R (LOW tercile +0.561R vs HIGH tercile +0.448R) '
   '-- medium confidence, re-run as the sample grows. See '
   'KNOWLEDGE_BASE.md and entry_ranking.py''s own comment at this term.',
   'Scoring', 'analysis/entry_ranking.py', 'float', '0.4', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
