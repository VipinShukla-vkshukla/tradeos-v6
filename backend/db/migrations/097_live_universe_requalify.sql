-- 097_live_universe_requalify.sql
-- 23-Aug-2026 (Stage D2, docs/TRADEOS_ROADMAP.md Track D)
--
-- The daily universe bench (~120 names, intraday_universe_bench_size) is
-- built once a day from YESTERDAY's turnover/ATR. A name too quiet
-- yesterday to qualify, then gapping hard today, is invisible all session.
-- These four keys arm the live re-check that closes that gap — see
-- intraday/scanner.py::movement_rejected_candidates()/live_requalify() and
-- intraday/engine.py::live_requalify_universe().
--
-- intraday_live_requalify_enabled ships FALSE: the check and its evidence
-- log run unconditionally regardless of this switch (same "compute and log,
-- act only when armed" shape floor_only_rank already uses) — this key only
-- gates whether a newly-qualifying name actually gets added to the live
-- bench and so reaches the websocket/detection engines.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_live_requalify_enabled', 'false',
   'Stage D2: admit a name outside today''s static universe bench once its '
   'OWN live numbers (not yesterday''s) clear the movement/turnover floor. '
   'The check always runs and logs what it would have admitted; this only '
   'gates whether it actually joins the live bench. Arm once the log shows '
   'real, sensible admissions over a stated period.',
   'Master controls', 'intraday/scanner.py', 'bool', 'false', 'MEDIUM'),

  ('intraday_live_requalify_interval_s', '45',
   'How often the live re-qualification check runs, in seconds. Its own '
   'timer, separate from the 300s full bench rebuild -- this is a handful '
   'of REST quote calls against a small, pre-filtered candidate list, not '
   'the expensive 1,800+-row historical scan.',
   'Master controls', 'intraday/config.py', 'int', '45', 'LOW'),

  ('intraday_live_requalify_move_pct', '1.20',
   'Today''s own |% change from previous close| a bench-excluded name must '
   'clear to be considered live-qualified. Matches intraday_min_atr_pct''s '
   'own default as the starting point -- same magnitude question, asked of '
   'today''s live number instead of yesterday''s ATR.',
   'Master controls', 'intraday/scanner.py', 'float', '1.20', 'LOW'),

  ('intraday_live_requalify_turnover_cr', '25.0',
   'Today''s own traded value (crore, so far) a bench-excluded name must '
   'clear to be considered live-qualified. Matches intraday_min_turnover_cr''s '
   'own default -- the same liquidity floor, checked against today''s own '
   'running total instead of yesterday''s full-day figure.',
   'Master controls', 'intraday/scanner.py', 'float', '25.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
