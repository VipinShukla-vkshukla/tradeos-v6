-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 021: intraday loss-cutting + paper auto-entry
-- ═══════════════════════════════════════════════════════════════════════════
--
-- A GAP THAT WOULD HAVE COST REAL MONEY
-- --------------------------------------
-- Intraday positions were being managed by the SWING exit policy. Three things
-- about that are wrong, and the third is dangerous:
--
--   target_r = 3.0        every intraday engine sets its OWN target — ORB 2R,
--                         PBK and VCE 2.5R, RNG the far edge of a proven range.
--                         A flat 3R holds past the level the setup was built on.
--
--   time_stop_days = 15   fifteen SESSIONS, on a trade that must be flat by
--                         15:20. A dead position rides the whole day and never
--                         trips a time stop.
--
--   invalidation          every Setup states the condition under which it stops
--                         being the trade it was — "a close back below the range
--                         high", "losing VWAP", "back inside the coil" — and
--                         NOTHING CHECKED IT. The entire reason those clauses
--                         were written is that an intraday thesis dies before
--                         the stop is reached. Positions were instead held to a
--                         stop two or three times further away.
--
-- The asymmetry that justifies cutting earlier: an intraday stop is 0.5-1.2%
-- against a 0.21% round trip. There is no room to be wrong slowly. A swing
-- position can spend a week being wrong and recover; an intraday position that
-- is wrong stays wrong, because the session ends.
--
-- AUTO-ENTRY, PAPER ONLY
-- ----------------------
-- Entries can now be taken automatically, but the gate is per framework AND per
-- mode, and LIVE auto-entry is a separate switch that defaults off. Promoting
-- paper to live must not silently also promote "simulate an entry" to "spend
-- money on an entry" — those are different decisions and deserve different
-- flips.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS invalidation_level numeric,
    ADD COLUMN IF NOT EXISTS invalidation_note  text;

COMMENT ON COLUMN public.open_positions.invalidation_level IS
  'Price whose loss kills the setup — range high, VWAP, coil high, PDH. Checked '
  'mechanically by intraday/exit_policy, which cuts here rather than waiting '
  'for a stop a full R further away.';


INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  -- ── Intraday exit policy ────────────────────────────────────────────────
  ('intraday_use_setup_target', 'true',
   'Exit at the target the ENGINE set, not a global multiple. ORB targets 2R, VCE 2.5R, RNG the far edge of its range — overriding those holds past the level the setup was built around.',
   'Intraday', 'intraday/exit_policy.py', 'bool', 'CRITICAL'),
  ('intraday_target_r', '2.0',
   'Fallback target multiple when a position carries no explicit target.',
   'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),
  ('intraday_partial_book_r', '1.2',
   'Book half here. Earlier than swing (1.5R) because an intraday move has hours, not weeks, to continue.',
   'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),
  ('intraday_partial_book_pct', '50', 'Fraction booked at the partial.', 'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),
  ('intraday_move_to_breakeven', 'true', 'Stop to entry once the partial is booked.', 'Intraday', 'intraday/exit_policy.py', 'bool', 'TUNING'),
  ('intraday_trail_r', '1.0', 'Trail width in R below the high-water mark.', 'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),
  ('intraday_trail_after_r', '1.5', 'Trailing arms here. Tighter than swing because there is less time for a retracement to resolve.', 'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),
  ('intraday_time_stop_minutes', '75',
   'MINUTES, not sessions. A position going nowhere is holding capital a working setup could use, and intraday there is no tomorrow to wait for.',
   'Intraday', 'intraday/exit_policy.py', 'int', 'CRITICAL'),
  ('intraday_time_stop_min_r', '0.3', 'Below this R after the time stop window, exit.', 'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),
  ('intraday_squareoff_buffer_min', '12',
   'Exit this many minutes before the close, on our terms rather than the broker''s.',
   'Intraday', 'intraday/exit_policy.py', 'int', 'CRITICAL'),
  ('intraday_check_invalidation', 'true',
   'Exit when the setup structure breaks, before the stop. This is the rule that cuts intraday losses early.',
   'Intraday', 'intraday/exit_policy.py', 'bool', 'CRITICAL'),
  ('intraday_invalidation_buffer_pct', '0.12',
   'Tolerance below the invalidation level. Intraday levels are tested constantly; a rule firing on every touch would exit every trade in its first ten minutes.',
   'Intraday', 'intraday/exit_policy.py', 'float', 'TUNING'),

  -- ── Auto-entry ──────────────────────────────────────────────────────────
  ('intraday_auto_entry', 'true',
   'Take intraday setups automatically. Honoured in PAPER; LIVE additionally requires intraday_live_auto_entry.',
   'Intraday', 'intraday/engine.py', 'bool', 'CRITICAL'),
  ('intraday_live_auto_entry', 'false',
   'Allow auto-entry to spend REAL money. Separate from intraday_auto_entry so promoting paper to live does not silently also promote "simulate an entry" to "buy something".',
   'Intraday', 'intraday/engine.py', 'bool', 'CRITICAL'),
  ('swing_auto_entry', 'false',
   'Take swing trade plans automatically. Off: the evening pipeline has full context you do not, and morning entries deserve a human look.',
   'Positions', 'control/position_lifecycle.py', 'bool', 'CRITICAL'),
  ('swing_live_auto_entry', 'false',
   'Allow swing auto-entry to spend real money.',
   'Positions', 'control/position_lifecycle.py', 'bool', 'CRITICAL'),
  ('paper_max_open_positions', '5',
   'Concentration cap for the paper book. A simulation that opens forty positions tests nothing about a system that can hold five.',
   'Positions', 'execution/paper_broker.py', 'int', 'TUNING')
ON CONFLICT (key) DO NOTHING;


DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Intraday positions now use intraday/exit_policy, not the swing policy.';
  RAISE NOTICE '  setup target · 75-min time stop · square-off buffer · INVALIDATION check';
  RAISE NOTICE 'Auto-entry: PAPER on, LIVE off for both frameworks.';
END $$;
