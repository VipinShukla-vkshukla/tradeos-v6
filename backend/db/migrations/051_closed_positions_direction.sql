-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 051: closed_positions.direction
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Migration 047 found open_positions had no direction column and every reader
-- of a position's direction was managing a short as a long for its entire
-- life. closed_positions has the identical gap one step later: the column
-- that RECORDS the outcome never carried direction either, and
-- control/position_lifecycle.close_position() computed realized_pnl as
-- (exit_price - entry) * qty and risk as entry - stop unconditionally —
-- correct for a long, sign-inverted and undefined (respectively) for a short.
--
-- It went unnoticed because it could not fire: intraday/engine.py's runway
-- gate for a short measured minutes_left against a session-close field that
-- did not exist on SessionState, defaulting to -10 at every hour of every
-- session, so no short had ever cleared it. SAPPHIRE on 2026-08-06 is the
-- first short position this system has closed. Its closed_positions row
-- shows realized_pnl=+51.90 for two legs the engine's own alerts recorded at
-- -0.85R and -0.42R — both losses, sign-flipped positive — and
-- r_multiple=NULL, because risk = entry - stop is negative for a stop
-- correctly placed ABOVE entry on a short and the guard against a negative
-- risk (`if stop0 and stop0 < entry`) discarded it as unknown rather than
-- computing it the other way.
--
-- A scan of all 102 rows in closed_positions for the short-shaped stop
-- (planned_stop_at_entry > entry_price) found exactly one match: SAPPHIRE.
-- The blast radius is one row, contained by the same gate that suppressed
-- every other short.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.closed_positions
  ADD COLUMN IF NOT EXISTS direction text NOT NULL DEFAULT 'LONG';

COMMENT ON COLUMN public.closed_positions.direction IS
  'LONG | SHORT, carried from open_positions.direction at close time. Read by '
  'control/position_lifecycle.close_position() to sign realized_pnl and the '
  'risk denominator correctly — a short''s stop sits ABOVE entry, so entry - '
  'stop is negative and must not be read as a wrong-signed risk rather than '
  'the other side''s risk. Every row before this migration is DEFAULT '
  '''LONG'', which is correct for all but SAPPHIRE (2026-08-06), corrected by '
  'hand alongside this migration since it predates the column existing.';

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'closed_positions.direction added, default LONG.';
  RAISE NOTICE 'SAPPHIRE (2026-08-06) is the one pre-existing SHORT row and was';
  RAISE NOTICE 'corrected by hand — see the accompanying code fix for why.';
END $$;
