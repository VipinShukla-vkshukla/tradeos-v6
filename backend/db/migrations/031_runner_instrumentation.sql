-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 031: make the runner decision observable (Stage 3)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE COLUMN THE CODE ALREADY READS
-- ---------------------------------
-- control/exit_rules.py:227 has always done this:
--
--     already = int(pos.get("runner_since_r") or 0)
--
-- There is no `runner_since_r` column. Not on open_positions, not on
-- closed_positions, not in any prior migration. `already` has therefore been 0
-- on every call since the rule was written.
--
-- It did not matter, because `already` is never used again. Neither is
-- `max_runners`, read from exit_max_runners two lines above it. **The cap on
-- concurrent runners is computed and then discarded** — exit_max_runners has
-- sat in system_config as a setting the operator can tune and that nothing has
-- ever enforced. Every position reaching the hard target with intact trend
-- becomes a runner, however many are already running.
--
-- WHY THE CAP COULD NOT HAVE BEEN WIRED WITHOUT THIS
-- --------------------------------------------------
-- Counting concurrent runners requires knowing which open positions ARE
-- runners, and nothing recorded that. Converting to a runner raised the trailing
-- stop and left no mark. So the missing column is not an oversight next to the
-- unused variable — it is the reason the variable stayed unused.
--
-- WHAT ELSE IS RECORDED, AND WHY
-- ------------------------------
-- Stage 3 has to separate two failure modes that look identical from outside:
--
--     the runner logic cannot fire, because its evidence inputs are empty
--     the runner logic fires and correctly declines
--
-- The first is a data-plumbing bug worth fixing; the second is the rule working
-- and must be left alone. Only `checks` and `verdict` at the moment of decision
-- tell them apart, so both are recorded at every target touch — including the
-- touches that decline, which are the ones that carry the information.
--
-- Additive only. Nothing is dropped, nothing is backfilled: rows written before
-- today legitimately do not know their runner state and NULL says so.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS runner_since_r   NUMERIC,
    ADD COLUMN IF NOT EXISTS runner_evidence  INTEGER,
    ADD COLUMN IF NOT EXISTS runner_verdict   TEXT;

COMMENT ON COLUMN public.open_positions.runner_since_r IS
  'The gain in R at which this position was converted to a runner. NULL means '
  'it is not a runner. This is the marker exit_rules.target_decision has read '
  'since it was written and that has never existed until migration 031.';

COMMENT ON COLUMN public.open_positions.runner_evidence IS
  'How many trend inputs were actually available when the runner decision was '
  'made (TrendQuality.checks). Below exit_runner_min_checks the decision was '
  'made on too little to justify holding, and the position was banked.';

COMMENT ON COLUMN public.open_positions.runner_verdict IS
  'STRONG | INTACT | FADING | BROKEN at the target touch. Recorded even when '
  'the position was banked, because a rule that declines correctly and a rule '
  'that cannot see are indistinguishable without it.';

ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS runner_since_r   NUMERIC,
    ADD COLUMN IF NOT EXISTS runner_evidence  INTEGER,
    ADD COLUMN IF NOT EXISTS runner_verdict   TEXT;

COMMENT ON COLUMN public.closed_positions.runner_since_r IS
  'Carried from open_positions at close so capture ratio can be measured '
  'separately for positions that ran and positions that were banked at target.';


-- ── The cap this makes enforceable ─────────────────────────────────────────
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('exit_runner_cap_enforced', 'false',
   'Whether exit_max_runners is actually applied. It has been read and '
   'discarded since the rule was written, so enforcing it CHANGES which '
   'positions run — false reproduces that original behaviour exactly. Turn on '
   'once runner_since_r has been populating for long enough that the count is '
   'trustworthy.',
   'Exit policy', 'control/exit_rules.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE n_open integer;
BEGIN
  SELECT count(*) INTO n_open FROM public.open_positions WHERE status = 'ACTIVE';
  RAISE NOTICE '';
  RAISE NOTICE 'Runner instrumentation ready. % active position(s) will start', n_open;
  RAISE NOTICE 'recording evidence and verdict at every target touch.';
  RAISE NOTICE '';
  RAISE NOTICE 'exit_max_runners is STILL not enforced — set exit_runner_cap_enforced';
  RAISE NOTICE 'to true only after runner_since_r has had time to populate, or the';
  RAISE NOTICE 'cap will count zero runners and refuse nothing.';
END $$;
