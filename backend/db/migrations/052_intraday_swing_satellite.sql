-- Layered under one_framework_per_symbol (migration 044), which stays the
-- master invariant and is UNCHANGED by this migration: it still refuses a
-- SWING entry into a name INTRADAY already holds, unconditionally, from the
-- same call site it always has.
--
-- This adds one narrower switch for the other direction: when SWING already
-- holds a name, let INTRADAY open a SAME-DIRECTION (LONG only — SWING is
-- CNC/LONG-only by construction) paper satellite in it instead of standing
-- down. See DESIGN_NOTES.md #1, "STATUS, 06-Aug-2026" for the full reasoning
-- and the two residual risks accepted while intraday stays PAPER
-- (intraday_live_auto_entry has no implementation): no per-symbol combined-
-- exposure cap across the two books yet, and one price move now informs two
-- learning populations within a single symbol, not just across the book
-- split in general.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_allow_swing_held_symbols', 'true',
   'Layered under one_framework_per_symbol, which stays the master invariant '
   'and still refuses a SWING entry into a name INTRADAY holds, unconditionally. '
   'This switch only relaxes the OTHER direction: when SWING already holds a '
   'name, INTRADAY may open a SAME-DIRECTION (LONG only -- SWING is CNC/LONG-'
   'only by construction) paper satellite in it rather than standing down. '
   'The SWING position is never touched -- exits are selected by (symbol, '
   'product), and square_off_paper only ever closes framework=INTRADAY, '
   'mode=PAPER rows. A SHORT intraday setup in a swing-held name is still '
   'refused unconditionally regardless of this switch -- shorting your own '
   'swing thesis reintroduces the two-contradicting-exit-ladders risk this '
   'system is built to avoid. Revisit if intraday_live_auto_entry is ever '
   'implemented: DESIGN_NOTES.md #1 explains why real capital in both legs is '
   'a materially different, harder question than a paper satellite next to a '
   'live core.',
   'Master controls', 'intraday/engine.py', 'bool', 'true', 'HIGH')
ON CONFLICT (key) DO NOTHING;
