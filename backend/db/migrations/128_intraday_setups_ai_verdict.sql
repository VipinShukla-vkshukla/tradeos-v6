-- 30-Aug-2026. The AVOID-hard-block fix (this same session, ai_advisor.py)
-- was quantified from cost_verdict='VETOED_AI' alone, because that is the
-- only AI verdict ever written to a setup row. PREFER/NEUTRAL never were --
-- they only ever nudged confidence in memory and survived, at most, in one
-- ai_context row per DAY (upserted, overwritten every slow tick). Reconstructing
-- from that gave 76 verdicts across 23 days, too thin to measure anything.
--
-- This does not fix that retroactively -- it starts the column so the next
-- few weeks of live PREFER/NEUTRAL/AVOID verdicts are queryable per setup,
-- the same way cost_verdict already is. Written by intraday/engine.py's
-- _record_setup() from this session's change, for every setup reached AFTER
-- ai_advisor.apply() ran (VETOED_AI, BELOW_CONVICTION, BLOCKED_LIQUIDITY,
-- BLOCKED_DEPTH, TAKEN, REJECTED_COST) -- NULL for setups blocked earlier in
-- the gate stack (BLOCKED_STRUCTURE etc.), which never reached the AI.
--
-- ai_source distinguishes a real LLM verdict ('ai'/'blend') from the
-- always-NEUTRAL empirical-prior fallback ('prior') -- collapsing those
-- would dilute the NEUTRAL bucket with rows the LLM never actually saw.
--
-- Nullable, no default. Applied directly via the Supabase MCP in the same
-- session this file was written; recorded here per this project's own
-- convention of one .sql file per migration.

ALTER TABLE public.intraday_setups
  ADD COLUMN IF NOT EXISTS ai_verdict text,
  ADD COLUMN IF NOT EXISTS ai_source text;

COMMENT ON COLUMN public.intraday_setups.ai_verdict IS
  'PREFER/NEUTRAL/AVOID from ai_advisor.py at the moment this setup was '
  'evaluated. NULL for rows written before 30-Aug-2026, or where no AI '
  'advice existed for this symbol, or where the setup was blocked before '
  'reaching the AI review step. See migration 128.';

COMMENT ON COLUMN public.intraday_setups.ai_source IS
  'ai/prior/blend -- whether ai_verdict came from the LLM call, the '
  'empirical per-engine prior fallback (always NEUTRAL), or both blended. '
  'See migration 128.';
