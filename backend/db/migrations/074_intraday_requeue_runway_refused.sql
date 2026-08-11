-- 11-Aug-2026. Seeds the system_config row for
-- intraday/engine.py::requeue_runway_refused_shorts(), shipped alongside
-- this migration in the same session. Learned from migration 073, applied
-- earlier the same day: code-level cfg_bool() defaults are safe with no row
-- present, but leave nothing for the operator to find or edit in Supabase —
-- so every new switch gets seeded here from now on, not left implicit.
--
-- WHAT IT DOES. can_short() refuses a NEW short once fewer than
-- intraday_short_min_runway_min (75) minutes remain before the cover
-- deadline -- an uncovered short goes to auction. That protects entries; it
-- says nothing about a genuinely good short whose setup simply matured too
-- late in the session to act on. requeue_runway_refused_shorts() runs once
-- per session, only in the OPENING phase (09:15-09:30, when runway is
-- obviously ample again), and re-seeds into today's universe only the
-- refused symbols whose price gapped DOWN again at today's open --
-- continuation the tape itself confirms, not a bare "detected yesterday and
-- not yet disproven". Re-seeded symbols go through the FULL pipeline again
-- (structure, cost, AI, allocator, can_short() itself) -- no bypass, no
-- shortcut; this only decides what gets looked at a second time.
--
-- DEFAULTS FALSE, same posture as exit_live_trend_ctx (migration 073) and
-- intraday_short_runway_tighten_enabled (shipped inert, same session):
-- unmeasured against this book's own outcomes yet. Arm after checking
-- `tradeos learn show` / discover_engines.py's refused_but_right(),
-- segmented on the runway gate specifically, shows these refusals actually
-- cost something.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_requeue_runway_refused', 'false',
   'When true, intraday/engine.py::requeue_runway_refused_shorts() runs '
   'once per session, in the OPENING phase only, and re-seeds into today''s '
   'universe any short refused for RUNWAY (not circuit-band/squeeze/'
   'liquidity) yesterday whose price gapped down again at today''s open. '
   'Re-seeded symbols go through the full pipeline again -- structure, '
   'cost, AI, allocator, can_short() -- no bypass. DEFAULT FALSE: unmeasured '
   'against this book''s own outcomes yet. See intraday/engine.py::'
   '_gap_continuation_shorts and _parse_runway_refusals for the pure core, '
   'tests/test_runway_requeue.py for the coverage.',
   'Master controls', 'intraday/engine.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
