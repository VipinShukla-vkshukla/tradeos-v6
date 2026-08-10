-- 10-Aug-2026. entry_ranking.score_plan()'s screener component (final_score)
-- was added at its full 0-100 magnitude with NO cfg_float weight at all --
-- the only component in the function missing one. A score of 80 contributed
-- +80 points by itself, more than every other component combined, so a plan
-- could outrank a better R:R / better-timed plan purely on a high screener
-- score, regardless of what final_score actually measures.
--
-- The KB's own tercile measurement (knowledge_base/KNOWLEDGE_BASE.md, 6-Aug)
-- found final_score flat against forward R: mean R by tercile 0.516 / 0.491
-- / 0.511, no monotonic separation, n=125 resolved CONTINUATION plans
-- (CTL/SEC/TPO/SBS/VBD/RSB/IAD). Medium confidence -- single family, and the
-- KB itself calls for a re-run as the resolved sample grows.
--
-- Code now centers the term at 50 (compute_msl's own mid-band boundary) and
-- weights it by this key, same pattern as rank_weight_tier /
-- rank_weight_conviction. Shipped at 0.3, not 0.0: unlike ai_tier (which had
-- NEVER been measured), final_score WAS measured and found flat -- but only
-- for one family, so this is a de-weight backed by medium-confidence
-- evidence, not a full zero-out backed by an unmeasured-component argument.
-- Raise toward 1.0 if a later tercile re-run finds separation; drop to 0.0
-- if it stays flat as the sample grows across the other engine families.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('rank_weight_screener', '0.3',
   'Weight of the screener''s own composite (final_score) in entry ranking, '
   'applied to the term AFTER it is centered at 50 (compute_msl''s own '
   'mid-band boundary) -- previously added unweighted at full 0-100 '
   'magnitude, silently dominating every other ranked component. Shipped '
   'low (0.3) on the KB''s own tercile finding that final_score does not '
   'separate forward R within the CONTINUATION family (n=125, 6-Aug-2026). '
   'Re-run python -m allocation.scoring --tercile as the resolved sample '
   'grows across other families before raising this.',
   'Scoring', 'analysis/entry_ranking.py', 'float', '0.3', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
