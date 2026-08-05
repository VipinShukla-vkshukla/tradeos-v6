-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 049: the allocator did not know a proposal could be SHORT
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Found during a pre-session readiness sweep, following the exact shape of
-- gap merge review already caught once in migration 047
-- (open_positions.direction) and once more in the same sweep
-- (intraday/engine.py's is_worth_taking() call missing direction=). This is
-- the third instance in the same feature: correct arithmetic, forgotten at a
-- caller.
--
-- allocation/proposal.py's Proposal dataclass had NO direction field at all.
-- from_intraday() adapted a short Setup into a Proposal that silently claimed
-- to be LONG. Proposal.coherent checked `0 < stop < entry < target`
-- unconditionally — the long-only shape — so every short proposal failed it
-- before allocation/scoring.py ever ran. Proposal.risk_per_share had the same
-- shape (`max(entry - stop, 0.0)`), returning 0 for a short. And
-- Allocator._prior_for()'s lookup ladder had no branch for the "/SHORT"
-- suffix scoring.intraday_priors() keys a short engine's population under, so
-- even a coherent short proposal would have been scored against
-- "INTRADAY/ALL" — the book fallback migration 044/047 made explicitly
-- LONG-only — rather than against any short's own measured distribution.
--
-- NONE OF THIS COULD HAVE TAKEN OR LOST MONEY. alloc_live_intraday is false
-- by default (migration 041) and there is no evidence it has been changed;
-- while it is false, allocator_permits() returns "not live for this book"
-- before ever consulting a verdict, for either direction. The actual damage
-- was to the EVIDENCE: every short proposal scored while alloc_shadow_enabled
-- is on (the default) would have been recorded with a wrong "incoherent
-- levels" decline, corrupting exactly the promotion evidence a future
-- decision to trust the allocator with real shorts would need to read.
--
-- THE FIX
--
-- Proposal gained a `direction` field (default LONG, so from_swing's implicit
-- behaviour is unchanged — swing is CNC-only and always LONG by construction,
-- and now says so explicitly). from_intraday() carries it from the Setup.
-- coherent and risk_per_share now call intraday.direction's shared functions
-- instead of maintaining a second, unsynchronised long-only implementation.
-- Allocator.select()'s scoring loop and _prior_for()'s lookup ladder are both
-- direction-aware, with a short falling back only within the short population
-- (…/SHORT, …/ALL/SHORT, …/NONE/SHORT) — never borrowing the long book's
-- distribution, which is the exact cross-class contamination
-- scoring.py's own header forbids.
--
-- allocation_decisions gains a `direction` column so the recorded verdict can
-- be told apart from the book's LONG proposals without cross-referencing
-- intraday_setups by symbol and timestamp — the same "a decision that cannot
-- be re-derived is a defect" principle allocation/hurdle.py's own docstring
-- states for hurdle_inputs.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.allocation_decisions
    ADD COLUMN IF NOT EXISTS direction text NOT NULL DEFAULT 'LONG';

COMMENT ON COLUMN public.allocation_decisions.direction IS
  'LONG | SHORT, carried from the Proposal that produced this verdict. '
  'Defaulted LONG so every pre-shorting row (100% of them) reads correctly. '
  'Lets a short verdict be told apart from a long one without joining back to '
  'intraday_setups by symbol and timestamp.';

CREATE INDEX IF NOT EXISTS idx_alloc_decisions_direction
    ON public.allocation_decisions (framework, direction, trade_date DESC);


DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Migration 049 applied. allocation_decisions.direction added.';
  RAISE NOTICE '';
  RAISE NOTICE 'Code fix in allocation/proposal.py and allocation/allocator.py:';
  RAISE NOTICE '  Proposal now carries direction; coherent/risk_per_share use';
  RAISE NOTICE '  intraday.direction instead of a long-only formula; the';
  RAISE NOTICE '  scorer and the prior lookup ladder are both direction-aware.';
  RAISE NOTICE '';
  RAISE NOTICE 'Nothing traded on this gap: alloc_live_intraday defaults false,';
  RAISE NOTICE 'so allocator_permits() never consulted a verdict either way.';
  RAISE NOTICE 'The damage was to shadow evidence, now stopped.';
  RAISE NOTICE '';
END $$;
