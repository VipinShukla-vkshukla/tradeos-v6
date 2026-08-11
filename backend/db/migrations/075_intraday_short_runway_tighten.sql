-- 11-Aug-2026. Seeds the system_config rows for
-- intraday/exit_policy.py::evaluate_intraday_exit() rung 6b, shipped in the
-- same session as migrations 073/074 -- and, like those, initially shipped
-- with only a cfg_bool()/cfg_float() code default and no row. Same gap,
-- same fix: found when the operator went looking for this exact key in
-- Supabase and it wasn't there, same as exit_live_trend_ctx before it.
--
-- WHAT IT DOES. Today, an open SHORT rides its full original stop width
-- right up to the hard cover-deadline square-off (exit_policy.py rung 1)
-- and is then closed at whatever price the market offers at that instant.
-- When armed, once fewer than intraday_short_min_runway_min (75, the SAME
-- number can_short() gates new entries on -- reused, not duplicated) minutes
-- remain before that short's own cover deadline, its stop tightens
-- continuously toward the live price instead. Linear from full risk width
-- at 75 minutes left down to intraday_short_runway_tighten_floor_pct of it
-- at zero (rung 1 already owns the zero case). NEVER LOOSENS a stop --
-- enforced by is_better_price(), the same invariant every other rung in
-- this ladder holds; turning the switch off stops NEW tightening but does
-- not restore a stop already moved in while it was on.
--
-- DEFAULTS FALSE, same posture as exit_live_trend_ctx (073) and
-- intraday_requeue_runway_refused (074): unmeasured against this book's own
-- outcomes yet. Paper-only (intraday), so no real capital is at risk while
-- it is being evaluated -- unlike exit_live_trend_ctx, which is swing/LIVE.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_short_runway_tighten_enabled', 'false',
   'When true, intraday/exit_policy.py::evaluate_intraday_exit() rung 6b '
   'tightens an open SHORT''s stop toward the live price as its cover '
   'deadline approaches, instead of riding full risk width into the hard '
   'square-off. Never loosens a stop -- only ever tightens sooner than the '
   'forced exit would have. DEFAULT FALSE: unmeasured against this '
   '(paper, intraday) book''s own outcomes yet. Reuses '
   'intraday_short_min_runway_min rather than a second runway number. See '
   'tests/test_intraday_short_runway_tighten.py.',
   'Exit policy', 'intraday/exit_policy.py', 'bool', 'false', 'MEDIUM'),
  ('intraday_short_runway_tighten_floor_pct', '40.0',
   'How tight rung 6b (intraday_short_runway_tighten_enabled) is allowed to '
   'pull an open short''s stop as its cover deadline reaches zero -- as a '
   'percent of the ORIGINAL risk width, not a price. Never reaches 0% on '
   'its own; the hard square-off at the deadline itself owns that case. '
   'Inert while intraday_short_runway_tighten_enabled is false.',
   'Exit policy', 'intraday/exit_policy.py', 'float', '40.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
