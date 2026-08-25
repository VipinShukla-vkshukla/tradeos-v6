-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 117: seed dashboard-writable config keys that had
-- no row, and were therefore un-settable from the Operator Panel
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 25-Aug-2026. The operator reported the Swing "Capital" field on the
-- dashboard reverting to ₹30,000 no matter what was typed, and the Risk
-- Exposure section not moving with it. Root cause: `swing_capital` had NEVER
-- had a row in system_config (unlike its sibling `intraday_capital`, seeded
-- 05-Aug-2026). The dashboard's PATCH endpoint (app/api/config/[key]/
-- route.ts) is deliberately UPDATE-ONLY — `.update().eq('key', k)` — so it
-- fails LOUDLY (404) rather than silently no-op'ing when a row is missing.
-- That hardening exists for a real reason (a prior Next.js 15 params bug
-- made every dashboard edit report success while changing nothing) and was
-- correctly left in place rather than weakened into an upsert, which would
-- let a typo'd or renamed key silently create a stray row instead of
-- failing loudly. Instead: audited every key the Operator Panel can write
-- (frontend/components/core/OperatorPanel.tsx, every `set(...)`/
-- `writeKey(...)` call site — 56 keys total, both frameworks) against
-- system_config and seeded the ones genuinely missing. Exactly two:
--
--   swing_capital           — seeded LIVE already, this session, at the
--                              operator's own requested figure (₹300,000).
--                              Included here for the durable record and so
--                              a fresh environment gets it too.
--   alloc_hurdle_cold_start — found by the same audit. Its OWN row was
--                              assumed to already exist by migration 044's
--                              UPDATE ('' = permissive, the safe cold-start
--                              default per that migration's own header) —
--                              that UPDATE matched zero rows for the exact
--                              same reason this migration exists. Reading
--                              currently falls through to hurdle.py's own
--                              cfg("alloc_hurdle_cold_start", "") default,
--                              which happens to equal the intended
--                              permissive value — so there has been no
--                              silent behavioural drift. But the dashboard's
--                              own "Cold-start bar" override field
--                              (OperatorPanel.tsx, shown explicitly because
--                              it is "the control that emptied the intraday
--                              book on 05-Aug") could never actually be
--                              used: every attempt to set a deliberate hard
--                              floor would 404 the same way swing_capital
--                              did. Seeded to '' (empty = permissive) —
--                              byte-for-byte the value already in effect
--                              via the code fallback, so this changes
--                              nothing about current behaviour, only
--                              whether the operator can ever override it.
--
-- Idempotent — ON CONFLICT DO NOTHING, safe to re-run.

INSERT INTO public.system_config
    (key, value, description, is_secret, category, subsystem,
     value_type, min_value, max_value, default_value, risk_level, deprecated)
VALUES
    ('swing_capital', '300000',
     'The capital SWING sizing runs against (config.capital_for). Never had a '
     'row before 2026-08-25 — the dashboard PATCH route is UPDATE-only, so every '
     'attempt to set this from the Operator Panel 404d silently and the Capital '
     'field / Risk Exposure section kept showing TOTAL_CAPITAL-derived figures '
     'instead. Seeded to mirror intraday_capital (set 2026-08-05). Set at the '
     'operator''s own requested figure. Currently PAPER-only effect: '
     'swing_trading_mode=PAPER, swing_live_auto_entry=false.',
     false, 'Positions', 'config.py (capital_for)', 'float',
     NULL, NULL, NULL, 'CRITICAL', false),

    ('alloc_hurdle_cold_start', '',
     'A deliberate hard floor for the bar while the allocator has too little '
     'history to have an opinion. EMPTY means permissive, which is the correct '
     'default: an allocator with no data must be indistinguishable from no '
     'allocator, or its first day in production is a shutdown (see migration '
     '044). Never had a row before 2026-08-25 — 044''s own UPDATE assumed one '
     'already existed and silently matched nothing, so the Operator Panel''s '
     '"Cold-start bar" override field could never actually be used even though '
     'reads already fell through to this same permissive default via '
     'hurdle.py''s cfg() fallback. Seeded empty — no behavioural change, only '
     'makes the existing dashboard control functional.',
     false, 'Allocation', 'allocation/hurdle.py', 'string',
     NULL, NULL, NULL, 'CRITICAL', false)
ON CONFLICT (key) DO NOTHING;
