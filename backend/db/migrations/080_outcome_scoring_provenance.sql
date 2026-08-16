-- 16-Aug-2026. F-27 mechanism A: intraday_setups rows scored from a session
-- that was still running, and no way to tell which ones.
--
-- THE DEFECT. `intraday/outcomes.py::resolve_day` prices a TIMEOUT at
-- `bars[-1]["close"]` -- the last bar it was handed -- and its work queue is
-- `.is_("outcome", "null")`, so a row it has written is never revisited. Both
-- are reasonable alone. Together they corrupt data, because of where the
-- function is called from: `intraday/run.py:416`, the daemon's `finally`
-- block, which runs on EVERY exit. A crash at 10:12, a Ctrl-C at 11:30, a
-- restart to pick up a config change -- each one asks Kite for the session so
-- far, scores every still-open setup TIMEOUT at a mid-morning price, and
-- freezes it. The evening pipeline's `backfill` does not come back for it; it
-- is no longer NULL.
--
-- That table is what `scoring.intraday_priors()` and `hurdle`'s arrival
-- distribution are both built from, so one frozen row prices every candidate
-- arriving after it. Measured signature on the live book: 58 same-window
-- contradictions, 42 of them a STOP and a TIMEOUT over overlapping windows of
-- one symbol -- one row scored against the whole session, one against a
-- truncated one, and NOTHING STORED SAYING WHICH WAS WHICH. That last clause
-- is what these three columns fix; the refusal itself is in code.
--
-- NO ROW IS RE-SCORED OR REPAIRED BY THIS MIGRATION, DELIBERATELY. The
-- contradictory pairs are the only evidence the defect exists, and the
-- population that will measure whether the guard worked. Overwriting them
-- destroys the measurement. Every existing row keeps outcome, outcome_pct and
-- three NULLs, and NULL provenance is itself the marker for "scored before
-- 16-Aug-2026, window end unknown".
--
-- WHY THREE COLUMNS AND NOT ONE.
--   scored_at       WHEN. Distinguishes a re-score from the original write.
--   scored_by       WHICH RUN. lease.instance_id() -- host-pid-uuid, the same
--                   string intraday_daemon_lease holds. A hostname alone
--                   cannot separate two daemons on one machine, or a daemon
--                   from the pipeline, and F-5/R-1 established that both
--                   happen here.
--   scored_through  THROUGH WHICH BAR. The timestamp of the last bar in the
--                   window that priced the outcome. This is the diagnostic
--                   that did not exist: a TIMEOUT whose scored_through reads
--                   11:30 on a session that ran to 15:30 is a frozen row,
--                   found in one query rather than by reasoning about which
--                   daemon died when.
--
-- The code sends these only after probing for them (one select per run, not
-- per row), so it is safe to deploy ahead of this migration -- PostgREST fails
-- the WHOLE update on one unknown column, and an outcome write lost to a
-- diagnostic column would be a worse bug than the one being diagnosed.

ALTER TABLE public.intraday_setups
  ADD COLUMN IF NOT EXISTS scored_at      timestamptz,
  ADD COLUMN IF NOT EXISTS scored_by      text,
  ADD COLUMN IF NOT EXISTS scored_through timestamptz;

COMMENT ON COLUMN public.intraday_setups.scored_at IS
  'When outcomes.resolve_day wrote this row''s outcome. NULL = scored before '
  'migration 080 (16-Aug-2026), window end unknown.';
COMMENT ON COLUMN public.intraday_setups.scored_by IS
  'lease.instance_id() of the run that scored it: host-pid-uuid. Separates two '
  'daemons on one host, and a daemon from the pipeline.';
COMMENT ON COLUMN public.intraday_setups.scored_through IS
  'Timestamp of the LAST BAR in the window that priced this outcome. A TIMEOUT '
  'is priced at that bar''s close, so a scored_through well before the session '
  'close is a row frozen at an intra-session price (F-27 mechanism A).';

-- The settling buffer between the market close and the moment scoring is
-- allowed. Kite publishes the closing minute a little after 15:30, so scoring
-- at 15:30:01 can still price a TIMEOUT at the 15:28 close -- the same defect,
-- one minute wide.
--
-- IT MUST STAY INSIDE THE DAEMON'S COOL-DOWN. run.py leaves its loop when
-- is_trading_session() goes false, at COOLDOWN_TO = 15:40, and calls
-- resolve_day on the way out. A buffer reaching 15:40 would mean the daemon
-- never scores its own session again and every day silently waits for the next
-- evening's pipeline. session_is_over() therefore CLAMPS this to 9 and logs a
-- WARNING rather than accepting a value that disables the path it protects.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('outcomes_close_buffer_min', '5',
   'Minutes after the 15:30 market close before intraday/outcomes.py will '
   'score that day''s detections. Exists because a TIMEOUT is priced at the '
   'last bar of the window, and the closing minute is published slightly after '
   'the close. CLAMPED to 9 (one minute inside the daemon''s 15:40 cool-down '
   'exit) with a WARNING if set higher, because a larger value would take the '
   'daemon out of the scoring path entirely. Past sessions are never subject '
   'to it. See tests/test_resolve_day_session_guard.py.',
   'Intraday', 'intraday/outcomes.py', 'int', '5', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
