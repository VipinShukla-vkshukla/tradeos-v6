-- 10-Aug-2026. VWR (VWAP reclaim) measured at n=34, gross -0.059R --
-- engine_scorecard's own "NO EDGE, signal problem" verdict. Operator's
-- instruction, verbatim: "I dont want to retire it unless you confirm there
-- is no fix and we need to retire" -- refined, not retired. Two concrete
-- logic bugs found by reading the engine against its own comments (see
-- intraday/strategies/vwap_reclaim.py for the full traces):
--
--   1. The "reclaim must be recent" check was a no-op in the common case --
--      it let a crossing several bars old pass, which is chasing an
--      established move, the exact thing its own docstring said it refused.
--      Fixed to a strict single-bar crossing. No new config; this changes
--      what "fresh" already meant, not a new tunable.
--
--   2. The target could be set past a level the stock had never traded at:
--      max(by_r, day_high*1.001) always picked the larger number, so a
--      day_high offering a real but PARTIAL (1-2R) move was discarded in
--      favour of an unproven 2R target. exit_policy.py's use_setup_target
--      rule exits AT this field when reached, so an unreachable target
--      means the trade rides the give-back guard down instead of banking a
--      real move -- this project's own recurring finding this session.
--      vwr_min_target_r is the new threshold: day_high must clear this many
--      R before it is trusted as the target; below it, the R-multiple
--      (vwr_target_r, unchanged at 2.0) stands in, same as before.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('vwr_min_target_r', '1.0',
   'Minimum R the day''s high must offer above entry before VWR trusts it as '
   'the target, instead of falling back to the vwr_target_r (2.0) R-multiple. '
   'Below this the day high is "too close to be worth the trip" -- the '
   'engine''s own original words -- and was already falling back correctly; '
   'above it, the proven level is now used even when it is less than a full '
   '2R away, which the previous max()-based logic discarded.',
   'Scoring', 'intraday/strategies/vwap_reclaim.py', 'float', '1.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
