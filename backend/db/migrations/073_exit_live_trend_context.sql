-- 11-Aug-2026. Seeds the system_config row for the switch introduced
-- alongside intraday/engine.py::refresh_trend_context() this same session --
-- the code shipped with a cfg_bool("exit_live_trend_ctx", False) default,
-- which is safe with no row present at all, but left no row to find or edit
-- in Supabase. That gap is exactly what this migration closes.
--
-- WHAT THE SWITCH GATES. Until this session, policy["_trend_ctx"] was
-- populated only inside manage_open_positions(), which runs once a day at
-- EOD. intraday/engine.py's live 15s loop called evaluate_exit() every
-- cycle, all session, with a policy dict that never carried the key -- so
-- exit_runners_enabled's "run the winner past 3R" and
-- exit_deterioration_enabled's "exit early if the thesis breaks while still
-- in profit" (both migration 019, both CRITICAL) were blind for the entire
-- trading day, every day, on the LIVE swing book. Neither fired incorrectly
-- -- TrendQuality.has_evidence requires >=3 checks, so zero evidence
-- correctly banks rather than false-running -- but neither could fire AT ALL
-- during market hours; both only ever got real evidence retroactively.
--
-- DEFAULTS FALSE, matching the code. This is a live-money behaviour change on
-- the swing book -- a position can now legitimately convert to a runner, or
-- exit early on deterioration, DURING market hours, neither of which could
-- happen before. Flip only after a session or two of the new WARNING log
-- ("no live trend context for N/M positions...") reads clean, confirming
-- load_signal_context() is reaching the broker/Supabase reliably on the
-- 300s slow timer this now runs on. See intraday/engine.py::
-- refresh_trend_context() for the full reasoning and
-- tests/test_exit_rules_live_vwap.py (registered in tools/verify.py) for the
-- 8 checks that assert the wiring, including the switch defaulting to
-- touching the database zero times when off.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('exit_live_trend_ctx', 'false',
   'When true, intraday/engine.py::refresh_trend_context() refreshes '
   'policy["_trend_ctx"] on the 300s slow timer instead of leaving it '
   'unset all session (previously only manage_open_positions() populated '
   'it, once a day at EOD). Governs whether exit_runners_enabled and '
   'exit_deterioration_enabled (both CRITICAL, migration 019) can actually '
   'fire DURING market hours rather than only being evaluated '
   'retroactively after the position has usually already closed at the '
   'fixed 3R target. Also attaches dist_vwap_live -- live price vs. '
   'session VWAP from the already-live tick overlay -- as the one genuinely '
   'live vote in assess_trend()''s check list. DEFAULT FALSE: this changes '
   'which live SWING exits fire, so it waits for an explicit flip after '
   'watching a session of the new "no live trend context for N/M '
   'position(s)" warning read clean. See control/exit_rules.py::'
   'assess_trend and intraday/engine.py::refresh_trend_context.',
   'Exit policy', 'intraday/engine.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
