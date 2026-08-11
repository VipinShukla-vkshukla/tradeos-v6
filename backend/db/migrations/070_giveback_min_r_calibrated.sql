-- 11-Aug-2026. intraday_giveback_min_r (migration 059, 1.0) sat above
-- where the closed book's losers actually peak. Direct query,
-- closed_positions, framework=INTRADAY, max_favorable_excursion IS NOT
-- NULL (n=27):
--
--   winners (n=10): MIN peak 0.499R, p25 0.849R, median 1.550R
--   losers  (n=17): p75 peak 0.490R, median 0.145R
--
-- Every winner in the sample peaked at >=0.499R at some point; 75% of
-- losers never reached 0.490R before turning into a loss. Those two
-- numbers coincide almost exactly, which is what makes 0.5 the boundary
-- the data itself draws. At the old 1.0R floor, a full quarter of winners
-- (p25 MFE 0.849R) never had the guard active during their most fragile
-- stretch.
--
-- Confirmed with tools/exit_ladder_replay.py --min-r 0.5 against the same
-- 20-day window: 9 positions would have been affected (vs 3 at min_r=1.0),
-- estimated ceiling +5.60R (vs +1.42R) -- KAYNES, SAPPHIRE, ADANIGREEN,
-- IFCI, HINDCOPPER, SWIGGY all peaked 0.5-1.1R and reversed to a loss via
-- SETUP_INVALIDATED/TIME_EXIT, never touching a rung the guard could have
-- pre-empted at the old floor. 0.45 was also tested (10 positions,
-- +5.75R ceiling) -- the marginal gain below 0.5 is small and has no
-- equivalent structural justification in the MFE quantiles, so 0.5 was
-- chosen as the data's own boundary rather than the value that happens to
-- maximise this specific historical replay.
--
-- n=27 is real but still thin (the same caveat migration 059 itself
-- carried at n=40 total closed, fewer with excursion data). Re-run
-- tools/exit_ladder_replay.py as the sample grows; see exit_policy.py's
-- own comment at this key and KNOWLEDGE_BASE.md.

UPDATE public.system_config
SET value = '0.5',
    description = 'Minimum peak R a position must have reached before the '
      'give-back guard can fire. LOWERED 1.0 -> 0.5, 11-Aug-2026, from the '
      'closed book''s own MFE quantiles -- winners'' minimum peak (0.499R) '
      'and losers'' 75th-percentile peak (0.490R) coincide almost exactly '
      'at this boundary. See migration 070 and exit_policy.py''s own '
      'comment at this key.',
    default_value = '0.5',
    updated_at = now()
WHERE key = 'intraday_giveback_min_r';

-- Defensive: register it if migration 059 somehow never landed on this
-- environment, so this migration is safe to run standalone.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
SELECT 'intraday_giveback_min_r', '0.5',
   'Minimum peak R a position must have reached before the give-back guard '
   'can fire. Calibrated from the closed book''s own MFE quantiles -- see '
   'migration 070.',
   'Master controls', 'intraday/exit_policy.py', 'float', '0.5', 'MEDIUM'
WHERE NOT EXISTS (
  SELECT 1 FROM public.system_config WHERE key = 'intraday_giveback_min_r'
);
