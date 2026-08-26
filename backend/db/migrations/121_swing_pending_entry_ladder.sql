-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 121: resting-limit swing entry ladder (Phase 3b)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 26-Aug-2026. Swing framework evolution blueprint, Phase 3b — the direct
-- fix for "RKFORGE quoted 720, then 717, never filled": a genuine
-- non-marketable resting limit instead of a fresh marketable chase every
-- cycle. New position status 'PENDING_ENTRY', distinct from the existing
-- 'PENDING_FILL' (that one is the seconds-scale submit-then-confirm gap;
-- this is a deliberate minutes-scale rest — see intraday/engine.py::
-- _resolve_pending_entries()).
--
-- Ships shadow-first: swing_pending_entry_enabled is 'false', so today's
-- marketable chase stays the live behaviour until real fill-price evidence
-- justifies arming it.

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS entry_ladder_step integer,
    ADD COLUMN IF NOT EXISTS entry_ladder_deadline timestamptz,
    ADD COLUMN IF NOT EXISTS entry_ladder_max_price numeric;

INSERT INTO public.system_config
    (key, value, description, is_secret, category, subsystem,
     value_type, min_value, max_value, default_value, risk_level, deprecated)
VALUES
    ('swing_pending_entry_enabled', 'false',
     'When true, a live swing entry rests a passive (non-marketable) LIMIT '
     'order at the current price instead of chasing through the offer, and '
     'is repriced in steps toward max_entry if unfilled '
     '(swing_pending_entry_ladder_steps / _step_minutes) before falling '
     'back to today''s ordinary marketable chase '
     '(swing_pending_entry_fallback_to_chase). Ships false — arm only once '
     'real fill-price evidence justifies it.',
     false, 'Execution', 'intraday/engine.py (_maybe_enter_swing, _resolve_pending_entries)', 'bool',
     NULL, NULL, 'false', 'HIGH', false),

    ('swing_pending_entry_ladder_steps', '3',
     'How many reprice steps a resting swing entry gets before the ladder '
     'is considered exhausted and the order is cancelled.',
     false, 'Execution', 'intraday/engine.py (_resolve_pending_entries)', 'int',
     1, 10, '3', 'MEDIUM', false),

    ('swing_pending_entry_step_minutes', '5',
     'Minutes a resting swing entry waits at its current price before the '
     'next ladder step (reprice or, if exhausted, cancel).',
     false, 'Execution', 'intraday/engine.py (_resolve_pending_entries)', 'int',
     1, 60, '5', 'MEDIUM', false),

    ('swing_pending_entry_fallback_to_chase', 'true',
     'When a resting entry''s ladder is exhausted without filling, attempt '
     'today''s ordinary marketable chase once, so a resting attempt can '
     'never cause a trade the system already decided to take to be missed '
     'entirely. False stands down instead once the ladder is exhausted.',
     false, 'Execution', 'intraday/engine.py (_chase_fill_fallback)', 'bool',
     NULL, NULL, 'true', 'MEDIUM', false)
ON CONFLICT (key) DO NOTHING;
