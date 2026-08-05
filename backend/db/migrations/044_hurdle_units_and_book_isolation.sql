-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 044: the bar the book could never clear
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHAT HAPPENED
--
-- 043 set alloc_live_intraday = true. From that moment the allocator held a
-- veto over the intraday book, and the intraday book took zero trades for a
-- full session while the swing book — whose veto was still false — traded
-- normally. Nothing reported an error. The logs read like a market with no
-- opportunities.
--
-- Three defects, stacked, each of which alone was enough:
--
--   1. UNITS. scoring.score() returns edge = (E[R] - cost_R) / hold_days:
--      expected R, NET of costs, PER DAY. hurdle._empirical_base built the bar
--      from outcome_pct / risk_pct over resolved detections: realised R, GROSS,
--      per trade. Two different quantities compared with `<`. Measured on a
--      population shaped like the real one, the 09:20 bar came to +1.20 against
--      an edge of -0.13 — a setup needed a prior mean of +1.41R to clear it and
--      the best engine in the book has never had one.
--
--   2. THE QUERY NEVER RAN. It selected `regime_at_detection`, a column on no
--      migration, written by no code, named in exactly one place in the
--      codebase. PostgREST rejects the entire request for one unknown column.
--      A bare `except` swallowed it and every call fell through to the cold
--      start. The "empirical bar" has never once been computed.
--
--   3. THE COLD START REFUSED. It returned 0.0, and 0.0 against a cost-netted
--      edge still declines everything: the intraday prior mean (+0.08R) does
--      not cover the MIS round trip (+0.21R). The module docstring promises a
--      cold start that AGREES with the live path so the plumbing is proved
--      before any opinion changes. It did the opposite.
--
-- AND THE CHECK THAT SHOULD HAVE CAUGHT (2) COULD NOT FAIL.
--
--   tools/validate_selects.py ends `return 1 if (problems and strict) else 0`.
--   health's check_selects called it as vs.main() — no argument, strict=False —
--   read only the return code, and printed "every SELECT names columns that
--   exist" directly over its own error output. That is the fifth
--   green-while-broken check found in this project.
--
-- WHAT THIS MIGRATION CHANGES
--
--   · allocation_decisions gains regime_bucket, so the bar can be segmented by
--     regime with a column rather than by parsing JSON. The code now reads its
--     arrival distribution from allocation_decisions.edge — the same column
--     scoring.score()'s output is written into — which is the one construction
--     under which the bar and the edge cannot drift apart again.
--
--   · alloc_live_intraday -> FALSE. The veto goes back to shadow. The intraday
--     book returns to the greedy path it ran before 043, which is the behaviour
--     that was producing trades. The allocator keeps scoring and recording; it
--     just stops refusing. Promote it again only after tools/allocator_report
--     shows verdicts that make sense on the corrected arithmetic — and note
--     that it now has a genuine cold start to serve out, because
--     allocation_decisions has no rows carrying a regime_bucket yet.
--
--   · one_framework_per_symbol -> true, and the rule is now enforced from BOTH
--     sides. It was written in two comment blocks and implemented in one
--     direction: intraday refused any name the swing book held, swing did not
--     check the intraday book at all. So a live CNC entry could be placed on
--     top of a paper MIS position — two exit ladders on one set of shares, a
--     third of the account on one thesis, and the same move scored twice by
--     the learning loop.
--
--   · alloc_hurdle_cold_start is now UNSET by default and means "permissive".
--     Set it only to impose a deliberate hard floor.
--
-- ORDER MATTERS: apply this BEFORE deploying the code. allocator._record now
-- writes regime_bucket, and PostgREST fails the whole insert on one unknown
-- column — the buffered flush would lose every verdict in the batch.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.allocation_decisions
    ADD COLUMN IF NOT EXISTS regime_bucket text;

COMMENT ON COLUMN public.allocation_decisions.regime_bucket IS
  'STRONG | WEAK, from hurdle.regime_bucket() at the moment of the decision. '
  'The hurdle segments its arrival distribution on this. Kept as a column '
  'rather than only inside hurdle_inputs because a segmentation that has to '
  'parse JSON to filter is one nobody will keep working.';

CREATE INDEX IF NOT EXISTS idx_alloc_decisions_bar_population
    ON public.allocation_decisions (framework, regime_bucket, trade_date DESC)
    WHERE edge IS NOT NULL;


-- ── Back to shadow. The veto was refusing everything. ──────────────────────
UPDATE public.system_config
   SET value = 'false', updated_at = now()
 WHERE key = 'alloc_live_intraday';

UPDATE public.system_config
   SET description = 'Let the allocator decide which intraday setups are taken. '
                     'Returned to OFF on 05-Aug-2026: promoted on 043, it declined '
                     'every setup for a full session because the bar was denominated '
                     'in gross R per trade and the edge in net R per day. The '
                     'arithmetic is fixed and the bar now reads the same column the '
                     'scorer writes. Promote again only after allocator_report shows '
                     'verdicts that make sense, and expect a cold start first: '
                     'allocation_decisions has no rows carrying a regime_bucket yet.',
       updated_at = now()
 WHERE key = 'alloc_live_intraday';


-- ── The cold start is permissive unless deliberately set ───────────────────
UPDATE public.system_config
   SET value = '', updated_at = now()
 WHERE key = 'alloc_hurdle_cold_start';

UPDATE public.system_config
   SET description = 'A deliberate hard floor for the bar while the allocator has '
                     'too little history to have an opinion. EMPTY means permissive, '
                     'which is the correct default: an allocator with no data must be '
                     'indistinguishable from no allocator, or its first day in '
                     'production is a shutdown. This was 0.0, which reads as harmless '
                     'and is not — against a cost-netted edge it refuses every '
                     'proposal whose expected R does not beat its own round trip.',
       updated_at = now()
 WHERE key = 'alloc_hurdle_cold_start';


-- ── One symbol, one book ───────────────────────────────────────────────────
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('one_framework_per_symbol', 'true',
   'Whichever book reaches a symbol first owns it until it closes. Migration '
   '028 made two rows for one name STORABLE — that is a storage guarantee, and '
   'it was read as a trading policy. Held in both books at once, the intraday '
   'square-off sells into a multi-week thesis at 15:15, the account carries a '
   'third of its capital on one idea across two sizing models that cannot see '
   'each other, and one price move is counted twice in the expectancy ledger. '
   'Turn this off only if you intend to trade a CNC core with an MIS tranche '
   'around it, and know that nothing downstream is built for it.',
   'Master controls', 'intraday/engine.py', 'bool', 'true', 'CRITICAL'),

  ('alloc_hurdle_lookback_days', '90',
   'How far back the hurdle reads scored arrivals when building its bar. Long '
   'enough for a usable sample, short enough that a regime from last quarter '
   'is not setting today''s bar.',
   'Scoring', 'allocation/hurdle.py', 'int', '90', 'TUNING')
ON CONFLICT (key) DO NOTHING;


-- ── alloc_max_slots is no longer the intraday book's cap ───────────────────
UPDATE public.system_config
   SET description = 'Fallback slot budget for callers that do not supply a '
                     'per-book one. NO LONGER the pooled cap across both books: it '
                     'was subtracted from every position entered today in EITHER '
                     'framework, so one swing entry in the morning capped the '
                     'intraday book — governed by intraday_max_new_per_day — at a '
                     'single slot for the rest of the session, and two entries of '
                     'any kind capped it at zero, at which point the hurdle returns '
                     'an infinite bar. Each book now brings its own budget.',
       updated_at = now()
 WHERE key = 'alloc_max_slots';


DO $$
DECLARE live_intra text; live_swing text; one_book text;
BEGIN
  SELECT value INTO live_intra FROM public.system_config WHERE key = 'alloc_live_intraday';
  SELECT value INTO live_swing FROM public.system_config WHERE key = 'alloc_live_swing';
  SELECT value INTO one_book  FROM public.system_config WHERE key = 'one_framework_per_symbol';

  RAISE NOTICE '';
  RAISE NOTICE 'Migration 044 applied.';
  RAISE NOTICE '  allocator veto:  intraday=%  swing=%   (both back to shadow)', live_intra, live_swing;
  RAISE NOTICE '  one book per symbol: %', one_book;
  RAISE NOTICE '';
  RAISE NOTICE 'The intraday book is back on the greedy path — the one that was';
  RAISE NOTICE 'producing trades before 043. The allocator keeps scoring and';
  RAISE NOTICE 'recording; it no longer refuses.';
  RAISE NOTICE '';
  RAISE NOTICE 'VERIFY, IN THIS ORDER:';
  RAISE NOTICE '    python -m tools.validate_selects --strict   must exit 0';
  RAISE NOTICE '    python -m tools.health                      hurdle + books must pass';
  RAISE NOTICE '    python -m tools.simulate                    what both books would do';
  RAISE NOTICE '';
  RAISE NOTICE 'Then after one session:';
  RAISE NOTICE '    python -m tools.allocator_report            do the verdicts read sane?';
  RAISE NOTICE '  Only then consider alloc_live_intraday = true again.';
  RAISE NOTICE '';
END $$;
