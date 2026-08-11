-- 11-Aug-2026. hurdle.py's own docstring already documents the first
-- attempt at a column named regime_at_detection: a query once tried to
-- read it, no migration had ever added it, no code had ever written it,
-- PostgREST rejected the whole request for the unknown column, and a bare
-- except swallowed the failure -- every call fell through to the cold
-- start. This is that column, built the other way round: added FIRST, and
-- written by intraday/engine.py::_record_setup() from this same session's
-- change, so it exists before anything tries to read it.
--
-- Nullable, no default -- old rows read as NULL (unknown), not a
-- fabricated value. Populates market_context.MarketContext.state
-- (RISK_ON/NEUTRAL/CAUTION/RISK_OFF) at the moment each setup was detected
-- or refused, for every verdict (TAKEN, every BLOCKED_*, every REFUSED),
-- not just taken trades -- the same "record every detection, not just
-- winners" principle intraday_setups already exists for.
--
-- WHAT THIS ENABLES, NOT WHAT IT DOES. Nothing reads this column yet.
-- allocation/scoring.py::regime_fit_multiplier() (migration 069) is the
-- mechanism that eventually will, gated at weight 0.0 pending the evidence
-- this column exists to accumulate -- see that migration and
-- KNOWLEDGE_BASE.md's "Promising Hypotheses" section.
--
-- Applied directly via the Supabase MCP in the same session this file was
-- written; recorded here so the migration history on disk matches what is
-- live, per this project's own convention of one .sql file per migration.

ALTER TABLE public.intraday_setups
  ADD COLUMN IF NOT EXISTS regime_at_detection text;

COMMENT ON COLUMN public.intraday_setups.regime_at_detection IS
  'market_context.MarketContext.state (RISK_ON/NEUTRAL/CAUTION/RISK_OFF) '
  'at the moment this setup was detected or refused. NULL for rows written '
  'before 11-Aug-2026 or where mc was unavailable. See migration 068.';
