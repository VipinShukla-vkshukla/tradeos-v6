-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 114: Track E, Stage E7 continuation — position
-- scale-in EXECUTION (docs/TRADEOS_ROADMAP.md, docs/FINDINGS.md F-78/F-80)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
--   control/position_lifecycle.py::evaluate_scale_in() (F-78, 24-Aug) has
--   been detection-only: it computes a real SCALE_IN decision and sizes it
--   through the real check_new_entry(), but nothing places the order or
--   writes the add back — deliberately, because how a combined position's
--   risk should be measured post-add was an unresolved accounting
--   question: does entry_price become a weighted average, or does the
--   add's own economics govern the R-multiple going forward while the
--   original tranche's already-secured gain stays untouched?
--
--   RESOLVED using a precedent already LIVE in this codebase, not invented
--   fresh: reconcile_with_broker()'s own QTY_INCREASED branch (control/
--   position_lifecycle.py) has for months updated current_qty/kite_qty/
--   actual_qty/invested_value on any quantity increase WITHOUT ever
--   touching entry_price. That is the exact "add's own economics kept
--   separate from the original tranche" shape the roadmap asked for — it
--   was just never named or extended to a system-INITIATED add before.
--   Scale-in execution follows the identical rule: entry_price,
--   planned_stop and active_sl — the three inputs evaluate_exit()'s
--   gain_r / giveback tiering / trailing-stop math reads — are NEVER
--   touched by an add. There is still only ONE active_sl per row (one
--   broker-side GTT per symbol), so the add does not get a second live
--   stop; its own risk-per-share at decision time is recorded in
--   scaled_in_stop purely as an audit/learning trail, mirroring what
--   scaled_in_price already is for the fill price.
--
-- TWO SWITCHES, BOTH OFF — building the execution path is a different
-- decision from arming it live, the distinction this whole track has held
-- since Stage C2 (build behind a switch, shadow-mode, before ever arming).
-- Naming mirrors swing_auto_entry / swing_live_auto_entry exactly.
--
-- Additive only. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS scaled_in         boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS scaled_in_qty      integer,
    ADD COLUMN IF NOT EXISTS scaled_in_price    numeric,
    ADD COLUMN IF NOT EXISTS scaled_in_stop     numeric,
    ADD COLUMN IF NOT EXISTS scaled_in_at       timestamptz,
    ADD COLUMN IF NOT EXISTS scale_in_order_id  text,
    ADD COLUMN IF NOT EXISTS scale_in_status    text;

COMMENT ON COLUMN public.open_positions.scaled_in IS
    'Has this position received its one Stage E7 add-on (confirmed filled)? '
    'Capped at one, per the roadmap''s own explicit limit — '
    'evaluate_scale_in() refuses a second add once this is true.';
COMMENT ON COLUMN public.open_positions.scaled_in_qty IS
    'Shares bought in the ONE confirmed add-on, distinct from original_qty.';
COMMENT ON COLUMN public.open_positions.scaled_in_price IS
    'The add-on''s own confirmed average fill price. Never blended into '
    'entry_price — see this migration''s own header.';
COMMENT ON COLUMN public.open_positions.scaled_in_stop IS
    'The add''s OWN risk-per-share (ltp - active_sl) at the moment it was '
    'sized. Audit/learning trail only — the position''s single active_sl '
    'still governs the whole combined quantity; this is never a second '
    'live stop.';
COMMENT ON COLUMN public.open_positions.scale_in_order_id IS
    'Kite order id for an add-on awaiting fill confirmation. Cleared once '
    'resolved. Mirrors entry_order_id''s role for a fresh entry.';
COMMENT ON COLUMN public.open_positions.scale_in_status IS
    'NULL normally; PENDING_FILL while an add-on order is awaiting broker '
    'confirmation. Deliberately separate from the row''s own status column '
    'so the original tranche stays ACTIVE and fully managed (exit ladder, '
    'GTT sync) while only the add resolves — status=PENDING_FILL on the '
    'row itself would hide the WHOLE position from every exit reader.';

INSERT INTO public.system_config (key, value, description) VALUES
  ('swing_scale_in_auto_entry', 'false',
   'Master switch: place a real add-on order when evaluate_scale_in() '
   'returns SCALE_IN. Mirrors swing_auto_entry''s two-switch shape exactly. '
   'Ships OFF with this migration — arming is a separate, explicit '
   'decision, not a side effect of this migration landing.'),
  ('swing_scale_in_live_auto_entry', 'false',
   'Second switch: may a scale-in add spend real money (vs a paper-'
   'simulated fill). Mirrors swing_live_auto_entry. Ships OFF.')
ON CONFLICT (key) DO NOTHING;
