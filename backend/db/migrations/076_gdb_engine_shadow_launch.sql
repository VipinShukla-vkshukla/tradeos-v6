-- 11-Aug-2026. Launches GDB (Gap-Down Bounce, intraday/strategies/
-- gap_down_bounce.py), built from brain_proposals#190 ("UNSEEN/gap down >
-- 1%": 34% of 41 symbol-days produced a 3.46%+ move against a 20%
-- background, 13 of those undetected by any engine, averaging 6.00% with
-- 92% closing strong -- a mean-reversion LONG bounce, not a short).
--
-- THIS ROW IS NOT OPTIONAL. registry.py::engine_lifecycle() defaults an
-- UNCONFIGURED engine to ACTIVE -- deliberately, so a forgotten config row
-- doesn't silently do nothing (see that function's own docstring). Without
-- this migration, GDB would go live as ACTIVE -- eligible for paper capital
-- -- the moment the code deploys, on zero scored outcomes. That directly
-- contradicts both this project's "propose, never auto-apply" posture and
-- the proposal's own recommendation (proposed_value = 'build as SHADOW').
--
-- SHADOW means: evaluates every cycle, records every detection to
-- intraday_setups exactly like an ACTIVE engine, is scored by
-- outcomes.resolve_day like any other -- but evaluate_all() never lets it
-- win the `best` (fundable) slot, so it cannot receive capital, paper or
-- otherwise. This is how every engine in this codebase is meant to earn
-- promotion: accumulate ~20 scored outcomes, then review before flipping
-- intraday_engine_gdb_lifecycle to ACTIVE.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_engine_gdb_enabled', 'true',
   'Master on/off for the GDB (Gap-Down Bounce) engine. True means it '
   'evaluates every cycle; see intraday_engine_gdb_lifecycle for whether '
   'its detections can receive capital. See intraday/strategies/'
   'gap_down_bounce.py.',
   'Master controls', 'intraday/strategies/registry.py', 'bool', 'true', 'LOW'),
  ('intraday_engine_gdb_lifecycle', 'SHADOW',
   'GDB (Gap-Down Bounce, brain_proposals#190) launch state. SHADOW: '
   'evaluates and records every detection, scored by outcomes.resolve_day '
   'like any other engine, but NEVER receives capital -- registry.py''s '
   'evaluate_all() only draws the fundable `best` setup from ACTIVE '
   'engines. Explicitly seeded here because registry.py''s own default for '
   'an unconfigured engine is ACTIVE, not SHADOW -- this row is what keeps '
   'a brand-new, unvalidated engine from taking capital on day one. '
   'Promote to ACTIVE only after reviewing ~20 scored outcomes, the same '
   'bar discover_engines.py sets for every SHADOW candidate it proposes.',
   'Master controls', 'intraday/strategies/registry.py', 'string', 'SHADOW', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
