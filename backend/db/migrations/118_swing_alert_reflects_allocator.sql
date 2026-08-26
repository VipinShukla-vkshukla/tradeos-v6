-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 118: swing entry alerts reflect the allocator's
-- live verdict, Phase 1 of the swing framework evolution blueprint
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 26-Aug-2026. RKFORGE fired repeated "BUY — in zone" Telegram alerts all
-- morning while the allocator DECLINE'd it every single cycle (edge -0.016
-- vs hurdle 0.029, negative net of cost) — the alert was built purely from
-- decide()/entry_ranking and never consulted self._verdicts, which
-- _allocate_shadow() had already populated earlier that same cycle, and
-- which _maybe_enter_swing's own allocator_permits() veto already reads a
-- few dozen lines later in the same call chain. act_on_candidates()
-- (intraday/engine.py) now looks up the same verdict before building the
-- alert: a DECLINE sends a distinct Action.kind ("ENTRY_DECLINED") stating
-- the real reason (edge vs hurdle) instead of "BUY", while a TAKE or
-- no-verdict (allocator off, or failed open) leaves today's alert exactly
-- as it was. No change to execution — _maybe_enter_swing is still called
-- unconditionally and still runs its own, independent allocator check.
--
-- Idempotent — ON CONFLICT DO NOTHING, safe to re-run.

INSERT INTO public.system_config
    (key, value, description, is_secret, category, subsystem,
     value_type, min_value, max_value, default_value, risk_level, deprecated)
VALUES
    ('swing_alert_reflect_allocator', 'true',
     'When true, a swing ENTRY alert that the allocator has DECLINEd this '
     'cycle is sent as kind=ENTRY_DECLINED (stating the real edge-vs-hurdle '
     'reason) instead of the normal BUY-shaped ENTRY alert. Reads '
     'self._verdicts, already populated by _allocate_shadow() earlier the '
     'same cycle — no new computation. Set false to revert to the prior '
     'behaviour (alert built purely from decide()/entry_ranking, blind to '
     'the allocator) instantly, without a code change.',
     false, 'Alerts', 'intraday/engine.py (act_on_candidates)', 'bool',
     NULL, NULL, 'true', 'MEDIUM', false)
ON CONFLICT (key) DO NOTHING;
