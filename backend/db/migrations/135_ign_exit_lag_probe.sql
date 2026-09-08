-- 09-Sep-2026. Instrumentation for the "does IGN need a faster/different
-- exit ladder" question (docs/FINDINGS.md, 09-Sep-2026 follow-on to the
-- bootstrap-override entry) -- built BEFORE that decision, not after,
-- because the decision needs real IGN trades to measure against and this
-- system stores no tick-level price history to reconstruct it later.
-- Building the ladder itself on a guess was explicitly rejected; this is
-- the alternative: measure first, decide once real data exists.
--
-- WHAT THIS MEASURES. intraday/event_core.py's 2-second loop now re-runs
-- exit_policy.evaluate_intraday_exit() -- the EXACT function the ordinary
-- 15s loop uses, never a second implementation -- against the freshest
-- tick, for any symbol that is a currently open INTRADAY/IGN position.
-- The FIRST time it would return a real action (not HOLD/TRAIL_SL), that
-- moment is timestamped. When the position actually closes (still only
-- ever decided by the ordinary 15s loop -- this probe changes NOTHING
-- about what happens to any position), the gap between the probe's
-- timestamp and the real close is exit_lag_seconds -- the honest, real-
-- data answer to "did 15s cost us anything, and how much."
--
-- STORAGE, DELIBERATELY BOUNDED. No new table, no per-tick row. Three
-- nullable columns, written ONCE per open position (the first crossing
-- only -- see IntradayEngine._ign_open_position()'s own "already probed"
-- check) and only for IGN. At IGN's own expected trade frequency this is
-- a handful of small writes a month, not a new storage line -- see
-- docs/FINDINGS.md, 09-Sep-2026, for the full sizing (naive per-tick
-- design would have cost ~4-6 MB/month; this design costs bytes).

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS exit_lag_action TEXT,
    ADD COLUMN IF NOT EXISTS exit_lag_probe_at TIMESTAMPTZ;

COMMENT ON COLUMN public.open_positions.exit_lag_action IS
    'IGN-only, advisory. The first exit_policy.evaluate_intraday_exit() '
    'action (EXIT_GIVEBACK/EXIT_TARGET/EXIT_STOP/BOOK_PARTIAL/...) the '
    '2-second probe observed for this position, ahead of the ordinary 15s '
    'loop actually acting on it. NULL until crossed once; never updated '
    'again after that (measures only the FIRST gap). Does not itself '
    'change anything about the position -- see event_core.py''s own '
    '"second exception" docstring section.';

COMMENT ON COLUMN public.open_positions.exit_lag_probe_at IS
    'When exit_lag_action was first observed. Carried through to '
    'closed_positions at close(), where exit_lag_seconds is computed '
    'against the real close time.';

ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS exit_lag_action TEXT,
    ADD COLUMN IF NOT EXISTS exit_lag_probe_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS exit_lag_seconds NUMERIC;

COMMENT ON COLUMN public.closed_positions.exit_lag_seconds IS
    'Real close time minus exit_lag_probe_at, in seconds -- how much '
    'earlier the 2-second probe would have acted vs the ordinary 15s '
    'cycle. NULL when no probe ever fired for this position (the common '
    'case -- most holds never cross an exit condition early). The single '
    'number the "does IGN need a faster exit ladder" decision should be '
    'made from: SELECT avg(exit_lag_seconds) FROM closed_positions WHERE '
    'exit_lag_seconds IS NOT NULL AND sub_engine = ''IGN''.';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_ign_exit_lag_probe_enabled', 'true',
   'IGN-only, purely advisory: re-runs evaluate_intraday_exit() on the '
   '2-second loop against open INTRADAY/IGN positions to measure whether '
   'the 15s cycle is slow to act on a real exit condition. Writes only '
   'exit_lag_action/exit_lag_probe_at (migration 135), once per position, '
   'and never influences any actual entry/exit/sizing decision -- default '
   'true because it changes no trading behaviour at all, unlike every '
   'other IGN-specific switch in this file. See docs/FINDINGS.md, '
   '09-Sep-2026.',
   'Master controls', 'intraday/event_core.py', 'bool', 'true', 'LOW')
ON CONFLICT (key) DO NOTHING;
