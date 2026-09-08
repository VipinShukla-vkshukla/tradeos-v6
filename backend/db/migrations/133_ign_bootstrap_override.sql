-- 08-Sep-2026. IGN (Ignition Momentum) has no INTRADAY/IGN prior of its own
-- yet (see migration 132) -- every proposal falls through to the pooled
-- INTRADAY/ALL prior, currently negative (n=302, mean_r=-0.1345), and a
-- realistic IGN candidate scores e_r ~ -0.27, which cannot clear
-- alloc_edge_absolute_floor=0.0. `score()`'s own formula never uses the
-- candidate's own R:R geometry, so IGN's own setup quality has zero
-- influence on whether it clears the bar right now.
--
-- NOT A PERMANENT TRAP. `intraday_setups.cost_verdict='TAKEN'` is set by
-- the cost gate, before the allocator ever runs, and survives an allocator
-- decline -- resolve_day() resolves every such row at the close of each
-- trading day via a real bar-walk regardless of whether a position was
-- ever opened, and intraday_priors() builds INTRADAY/IGN from exactly
-- those. IGN's own prior starts accumulating on its own the moment a
-- trading day closes; today's n=0 is because IGN has not lived through a
-- session close yet, not a structural dead end. What THIS migration buys
-- is speed and fidelity, not survival: a small number of genuinely
-- executed paper round trips (real fill, real exit ladder, real partial
-- booking/trailing/giveback behaviour) is a materially better signal than
-- the organic path's idealised bar-walk estimate, faster. See
-- docs/FINDINGS.md, 08-Sep-2026, for the full reasoning and the operator's
-- own explicit choice after being walked through both framings.
--
-- BOUNDED AND LIFETIME (10, ever) -- not a daily reset. Self-obsoletes
-- once INTRADAY/IGN's own prior crosses priors_min_sample_intraday (30)
-- and the allocator starts pricing IGN on real evidence; nothing here
-- changes that gate for any other engine, or the allocator's own
-- edge/hurdle logic, or any other gate the override sits downstream of
-- (cost, liquidity, depth, structure, conviction, shortability, re-entry,
-- cross-framework) -- see intraday/engine.py::act_on_setups() and
-- intraday/event_core.py::_try_ign_fast_entry() for exactly where the
-- override sits and what it does and does not touch.

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS bootstrap_override_slot INTEGER;

COMMENT ON COLUMN public.open_positions.bootstrap_override_slot IS
    'NULL for every ordinary entry. 1..N (intraday_ign_exploration_trades) '
    'for one of IGN''s bounded lifetime bootstrap-override entries -- an '
    'entry that proceeded despite an allocator DECLINE/DEFER because IGN '
    'has no dedicated prior of its own yet. The authoritative, queryable '
    'record: SELECT * FROM closed_positions WHERE bootstrap_override_slot '
    'IS NOT NULL finds every one, in order, from the database alone. See '
    'intraday/engine.py::_maybe_open_paper()/_ign_bootstrap_used_count().';

ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS bootstrap_override_slot INTEGER;

COMMENT ON COLUMN public.closed_positions.bootstrap_override_slot IS
    'Carried through from open_positions at close() -- see that column''s '
    'own comment.';

-- IGN-specific, ON CONFLICT DO NOTHING (not DO UPDATE), matching migration
-- 132's own treatment of IGN-specific keys the operator may deliberately
-- retune later -- never silently re-armed by a re-run. 0 disables the
-- mechanism entirely (every IGN decline then behaves exactly as before
-- this migration); no separate on/off switch needed, matching this file's
-- own existing intraday_max_entries_before_time "0 disables" convention.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_ign_exploration_trades', '10',
   'IGN-only, LIFETIME (not daily) cap on bootstrap-override entries -- '
   'paper positions opened despite an allocator DECLINE/DEFER because IGN '
   'has no INTRADAY/IGN prior of its own yet (see migration 132/133 and '
   'docs/FINDINGS.md, 08-Sep-2026). Read by '
   'IntradayEngine._ign_bootstrap_used_count() (intraday/engine.py) and '
   'consulted from both act_on_setups() and '
   'event_core.py::_try_ign_fast_entry(). Every other gate (cost, '
   'liquidity, depth, structure, conviction, shortability, re-entry, '
   'cross-framework) still applies in full -- this overrides ONLY the '
   'allocator''s edge/hurdle verdict, and only for IGN, and only while '
   'slots remain. 0 disables the mechanism entirely.',
   'Master controls', 'intraday/engine.py', 'int', '10', 'HIGH')
ON CONFLICT (key) DO NOTHING;
