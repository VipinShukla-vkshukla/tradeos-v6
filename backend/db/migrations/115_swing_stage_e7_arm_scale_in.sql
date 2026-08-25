-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 115: Track E, Stage E7 arming pass
-- (docs/FINDINGS.md F-82)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Operator's own explicit instruction, same session as migration 114:
-- "arm the necessary switches to keep this new capability alive in live
-- market." Applied directly against the running database first (this
-- file is the durable record of that change, matching migration 113's
-- own precedent for an arming pass — the switches already read true by
-- the time this file lands).
--
-- EVIDENCE BEHIND THIS ARM, NAMED EXPLICITLY RATHER THAN LEFT IMPLIED —
-- the same discipline migration 113's own header used for its four
-- thin-evidence switches: ZERO real-world firings. Unlike 113's
-- "seasoned" switches, scale-in execution has never fired against a
-- real position even in shadow — F-78's own quantify pass found only 2
-- of 17 recent closed SWING trades ever crossed the 1.0R runner line at
-- their peak. The live book at the time of arming (25-Aug-2026) held
-- one open SWING position, HAL, sitting BELOW its own entry price —
-- nowhere near eligible. Arming has no immediate effect; it takes hold
-- the next time a live SWING position clears all four of
-- evaluate_scale_in()'s rails, which the book's own history says is
-- rare.
--
-- Idempotent — re-running when already 'true' changes nothing.
UPDATE public.system_config
   SET value = 'true'
 WHERE key IN ('swing_scale_in_auto_entry', 'swing_scale_in_live_auto_entry');
