-- 08-Sep-2026. Launches IGN (Ignition Momentum, intraday/strategies/
-- ignition.py) ACTIVE in the paper book, with its own fast-entry path on
-- the 2-second event_core loop -- the operator's own explicit choice, made
-- with full awareness this is a zero-scored-outcome arm, after confirming:
--   (a) intraday stays paper-only regardless -- no real money moves, since
--       intraday_live_auto_entry has no implementation in this codebase.
--   (b) the allocator itself adds no meaningful latency -- Allocator.
--       select() is in-memory, microseconds, and already runs every 15s
--       cycle, not on a slower timer. The 15s cycle INTERVAL was the real
--       bottleneck for a fast-moving setup, closed by (c) below instead of
--       by touching the allocator's own logic.
--   (c) event_core.py's 2s loop (Stage D3, built 24-Aug-2026, never
--       previously armed) already detects that fast; it just never acted.
--       intraday_ign_fast_entry_enabled scopes acting on it to IGN alone --
--       every other engine continues shadow-logging only, unchanged.
--
-- Same shape of decision this project has already made twice: the
-- 07-Aug-2026 VCE/RNG promotion (ACTIVE on measured-negative history,
-- because intraday's own edge hurdle is the real gate, not the lifecycle
-- flag) and F-82 (swing scale-in, armed with zero prior live firings, on
-- the operator's own explicit instruction). See docs/FINDINGS.md,
-- 08-Sep-2026 ("Intraday trader review" and its follow-on build entries),
-- for the full quantification (121 symbol-days with an >=8% move over 31
-- days, 92 of them undetected by any of the 8 existing engines) and the
-- can_short()/fast-path safety-gap trace that shaped this design.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_engine_ign_enabled', 'true',
   'Master on/off for the IGN (Ignition Momentum) engine -- evaluates '
   'every cycle; see intraday_engine_ign_lifecycle for whether it can '
   'receive capital. intraday/strategies/ignition.py.',
   'Master controls', 'intraday/strategies/registry.py', 'bool', 'true', 'LOW'),

  ('intraday_engine_ign_lifecycle', 'ACTIVE',
   'IGN (Ignition Momentum) launch state -- ACTIVE from day one, on zero '
   'scored outcomes, the operator''s own explicit instruction (see '
   'docs/FINDINGS.md, 08-Sep-2026). Same reasoning as the 07-Aug-2026 '
   'VCE/RNG promotion: intraday stays paper, so the allocator''s own edge '
   'hurdle is the real gate, not the lifecycle flag. Watch its scored '
   'outcomes closely -- this is real paper capital allocated with no '
   'prior track record. Unlike a SHADOW-launched engine (GDB), this row '
   'is not overriding a dangerous unconfigured default -- registry.py''s '
   'own default for an unconfigured engine is ALSO ACTIVE -- it is shipped '
   'explicitly for auditability, not because omitting it would change '
   'behaviour.',
   'Master controls', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'HIGH'),

  ('intraday_event_core_enabled', 'true',
   'Arms the 2-second tick-triggered detection loop (Stage D3, built '
   '24-Aug-2026, never previously armed). Required for IGN''s fast-entry '
   'path to run at all. Every OTHER engine continues shadow-logging only, '
   'to intraday_event_shadow, exactly as already built and verified in '
   'Stage D3 -- this switch does not change their capital eligibility, '
   'only whether the 2s loop runs at all.',
   'Master controls', 'intraday/event_core.py', 'bool', 'false', 'MEDIUM')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
    description = EXCLUDED.description, risk_level = EXCLUDED.risk_level;

-- Separate INSERT, deliberately: this key is IGN-specific and must never
-- be confused with the general intraday_event_core_enabled arm above --
-- ON CONFLICT DO NOTHING here (not DO UPDATE) so re-running this migration
-- never silently re-arms a switch the operator may have since turned off
-- to investigate a live issue.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_ign_fast_entry_enabled', 'true',
   'IGN-only: act on a TAKE verdict from the 2-second event_core loop '
   'instead of waiting for the ordinary 15s cycle. Armed with zero prior '
   'live firings, the operator''s own explicit instruction -- the '
   'allocator itself was confirmed to add no meaningful latency '
   '(Allocator.select() is in-memory, microseconds); this switch exists '
   'so the 15s cycle interval -- the actual bottleneck -- does not cost a '
   'fast-moving setup its window. No other engine reads this key. See '
   'intraday/event_core.py::_try_ign_fast_entry() for the exact pipeline '
   'this runs -- the same shortability/re-entry/cross-framework/event/'
   'structure/AI/conviction/sizing/liquidity/depth/cost gates the ordinary '
   '15s loop applies to every candidate, reused via '
   'IntradayEngine._evaluate_one_intraday_candidate(), never a narrower '
   'or reimplemented copy.',
   'Master controls', 'intraday/event_core.py', 'bool', 'false', 'HIGH')
ON CONFLICT (key) DO NOTHING;

-- No rows for any ign_* tunable (ign_min_pct, ign_target_r, etc.) -- cfg_
-- float()/cfg_int() fall back to the Python-side default in ignition.py
-- when no system_config row exists, same as every gap_*/pbk_* key. No
-- schema change -- intraday_setups already has every column a new
-- engine's Setup needs, and intraday_event_shadow (migration 105) already
-- has every column event_core.py's shadow-log path writes.
