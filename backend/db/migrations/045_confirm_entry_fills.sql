-- 045: confirm entry fills before treating a swing buy as a real position
--
-- WHY THIS EXISTS
-- ----------------
-- execution/order_manager.py's place() returns success the moment Kite hands
-- back an order_id — that confirms the order was SUBMITTED, not that it
-- FILLED. Swing entries are LIMIT day orders; a limit that never trades
-- through its price simply expires unfilled at session close. Nothing
-- checked for that, so _maybe_enter_swing() wrote the position straight to
-- status='ACTIVE' with the requested limit price as entry_price — a genuine
-- position, GTT stop and all, on shares that were never bought.
--
-- TMCV, 03-Aug-2026: bought (submitted) at 09:47 IST, GTT placed three
-- minutes later. From the very next morning the exit logic tried to sell it
-- 260 times over the session and was refused every time — "broker shows 0
-- held" — because there was nothing to sell. Two days later reconcile found
-- it absent from holdings, guessed a sale had happened, and closed it with a
-- fabricated +Rs 124.44 gain that had already fed into signal_log's outcome
-- columns and generated a "lesson" the AI decision context had applied four
-- times before anyone looked.
--
-- THE FIX
-- -------
-- A new status, 'PENDING_FILL', sits between order placement and confirmed
-- ownership. Every existing reader of open_positions already filters
-- status='ACTIVE' (see position_lifecycle.py's own note: "every reader
-- filters status='ACTIVE'") — so a PENDING_FILL row is automatically
-- invisible to exits, GTT sync, alerts, candidate ranking and reconcile's
-- own "in DB, not in Kite -> sold" logic, with no changes needed to any of
-- them. entry_order_id is what a later pass (engine._resolve_pending_fills,
-- on the 300s slow timer) uses to ask Kite what actually happened: COMPLETE
-- promotes the row to ACTIVE with the real fill price and quantity;
-- REJECTED or CANCELLED deletes it — nothing was ever bought, so nothing
-- should be recorded as a position.
--
-- Not gated behind a new system_config switch: this does not add or widen
-- a money-movement capability, it corrects an existing one that already
-- runs behind swing_auto_entry and swing_live_auto_entry. A bug in this
-- code fails toward "position stays unconfirmed and gets flagged stale",
-- never toward "phantom position created" or "real fill silently dropped".

ALTER TABLE open_positions
  ADD COLUMN IF NOT EXISTS entry_order_id text;

COMMENT ON COLUMN open_positions.entry_order_id IS
  'Kite order_id for the entry order, set while status=PENDING_FILL. '
  'Used by engine._resolve_pending_fills() to confirm the fill via '
  'kite.order_history() before the position is treated as real. '
  'Cleared (left as-is) once promoted to ACTIVE — kept for audit.';
