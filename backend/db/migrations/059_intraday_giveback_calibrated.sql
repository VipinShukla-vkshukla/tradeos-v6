-- 10-Aug-2026. Enables the intraday give-back guard, which shipped OFF in
-- exit_policy.py with an explicit calibration trigger written into its own
-- comment: "set intraday_giveback_pct once ~20 intraday positions have closed
-- carrying excursion data, and calibrate it on those rather than on the swing
-- number, which comes from a different horizon."
--
-- tools/exit_audit.py, first run against the live book: 40 closed INTRADAY
-- positions with a stop and an R on record -- past the trigger. Of the 26
-- losers, several were up more than 0.3R before turning (the exact shape the
-- guard exists to catch), and separately tools/engine_scorecard.py confirms
-- realised profits are averaging far below the available move on winners.
--
-- THE EXACT PERCENTAGE IS NOT YET RE-CALIBRATED ON CLEAN DATA. The first
-- exit_audit run also surfaced a data-quality bug in the tool itself --
-- several exit reasons showed |MFE R| over 100, which is not physically
-- reachable against a real stop and comes from planned_stop_at_entry sitting
-- implausibly close to entry on some rows (fixed the same session, see the
-- tool's own history). So this migration deliberately does NOT invent a
-- number from the corrupted run. It mirrors SWING's setting instead --
-- exit_giveback_pct=50, the one this codebase has an actual 70-trade study
-- behind (24 of 28 losing swing trades had been >0.5% in profit first,
-- KNOWLEDGE_BASE.md) -- as the documented starting point the code comment
-- asked for, pending a clean re-run of exit_audit to refine it specifically
-- for the intraday horizon.
--
-- giveback_min_r stays at intraday's EXISTING code default (1.0), not
-- swing's 0.5 -- intraday's own docstring already argues for this
-- specifically ("an intraday position has LESS time to recover, not more"),
-- and unlike the percentage this was a considered choice already made in
-- code, not an unset placeholder. Registered explicitly here so the value is
-- visible in system_config rather than only in a function default.

-- THE DELIBERATE CHANGE. DO UPDATE: this is the one value this migration
-- exists to move, from OFF to the swing-mirrored starting point, regardless
-- of what it currently holds.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_giveback_pct', '50.0',
   'Give-back guard for INTRADAY positions, mirroring SWING''s proven '
   '50% (exit_giveback_pct) as the calibration starting point named in '
   'exit_policy.py''s own comment -- set once ~20 closed intraday positions '
   'carry excursion data. 40 are on record as of 10-Aug-2026. Re-measure '
   'with tools/exit_audit.py after this has run a few sessions and tighten '
   'or loosen from here; the 50% figure is inherited from swing''s 70-trade '
   'study, not yet independently calibrated on intraday''s own outcomes.',
   'Master controls', 'intraday/exit_policy.py', 'float', '0.0', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET
  value = EXCLUDED.value,
  updated_at = now();

-- DOCUMENTATION ONLY. DO NOTHING: unlike giveback_pct, this value is not
-- changing -- it already matches exit_policy.py's own code default (1.0).
-- Registered so it is visible in system_config rather than only in a
-- function signature; if an operator has already set this key to something
-- else, that choice is left alone.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_giveback_min_r', '1.0',
   'Minimum peak R a position must have reached before the give-back guard '
   'can fire. Intentionally STRICTER than swing''s 0.5 -- intraday has less '
   'time to recover a faded move, so the guard should not trigger on small '
   'early excursions the way it reasonably can on a multi-week swing hold. '
   'This was already the code default in exit_policy.py; registered here so '
   'it is visible in system_config rather than only in a function signature.',
   'Master controls', 'intraday/exit_policy.py', 'float', '1.0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
