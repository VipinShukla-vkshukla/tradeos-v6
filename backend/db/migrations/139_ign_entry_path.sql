-- 12-Sep-2026. HINDALCO (11-Sep) closed with bootstrap_override_slot=None
-- and its only allocation_decisions row reading DECLINE -- looking like an
-- unexplained trade from the position record alone. Traced the real cause:
-- IGN's fast-entry path (event_core.py::_try_ign_fast_entry(), migration
-- 132) scores a single candidate through Allocator._score_proposals()
-- directly, which is explicitly documented as "no database write... safe
-- to call from a 2-second loop for exactly that reason" -- so a genuine
-- fast-path TAKE leaves no trace in allocation_decisions at all, and
-- bootstrap_override_slot stays NULL for the same reason it stays NULL on
-- an ordinary ONE, because neither this nor a real organic approval used
-- the override. Today, `bootstrap_override_slot IS NULL` conflates two
-- genuinely different things: "the ordinary competitive pass approved
-- this" and "the fast lane's own separate, single-candidate pass approved
-- this" -- currently indistinguishable without re-deriving it from source,
-- which is what this session did twice in two days (PINELABS, HINDALCO).
--
-- Fix is a label, not a gate: which of the two entry mechanisms/four real
-- states opened a position, stamped once at write time, changing no
-- decision anywhere. Same shape and same safety story as
-- bootstrap_override_slot itself (migration 133) -- a new NULLable column,
-- nothing reads it today, so nothing can be affected by it existing.

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS entry_path TEXT;

COMMENT ON COLUMN public.open_positions.entry_path IS
    'Which write path opened this position -- ''ordinary'' (the normal '
    '15s competitive allocator pass), ''bootstrap'' (act_on_setups(), an '
    'IGN lifetime bootstrap-override slot, migration 133), ''fast_organic'' '
    '(event_core.py::_try_ign_fast_entry(), a genuine single-candidate '
    'TAKE that never touches allocation_decisions), or ''fast_bootstrap'' '
    '(the fast path''s own bootstrap-override branch). NULL for any row '
    'written before this migration. See intraday/engine.py::'
    '_maybe_open_paper() and intraday/event_core.py::_try_ign_fast_entry() '
    'for exactly where each value is set.';

ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS entry_path TEXT;

COMMENT ON COLUMN public.closed_positions.entry_path IS
    'Carried through from open_positions at close() -- see that column''s '
    'own comment.';
