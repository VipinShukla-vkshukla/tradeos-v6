-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 043: Phase 4 live
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 1. THE PHASE GATE: 3 -> 4
--
-- Verified before touching it, because I refused this once already and the
-- refusal was caution rather than evidence. Every reader compares with a LOWER
-- bound and there is no equality test and no upper bound anywhere:
--
--     ml_regime_classifier.py:651   if phase < 2
--     execution_engine.py:52,128    if phase < 3
--     gates.py / intraday/config    >= 3.0 (orders), >= 2.5 (GTT)
--     run_pipeline.py:404,452       >= 2, >= 1
--
-- So 4 satisfies every gate exactly as 3 did. The bump is functionally a no-op
-- and semantically the point: the banner now says what the system is.
--
-- 2. PEAD AND ACC -> ACTIVE
--
-- Their scores now enter composite_score instead of being logged beside it.
-- ACC produced 54 candidates against the live universe in testing, so this
-- widens the swing shortlist materially from the next pipeline run. PEAD will
-- stay quiet until the seeded results_calendar dates actually pass — that is
-- the calendar being honest, not the engine being broken.
--
-- 3. THE ALLOCATOR: LIVE ON INTRADAY, NOT YET ON SWING
--
-- alloc_live_intraday -> true. alloc_live_swing stays FALSE for one session.
--
-- Until today `alloc_live_*` set a column value in allocation_decisions and
-- nothing read it: flipping it would have produced a system REPORTING itself
-- as live-allocating while the greedy path still chose every entry. That is
-- the silent-success failure this project has hit four times. This migration
-- ships alongside the consumer that makes the switch mean what it says —
-- IntradayEngine.allocator_permits(), vetoing in act_on_setups and
-- act_on_candidates.
--
-- Intraday first because it is the PAPER book: if the allocator's very first
-- production output is wrong, being wrong costs nothing. Swing is real money
-- and follows after one session of readable evidence.
--
-- The veto FAILS OPEN by design. A proposal the allocator never scored is
-- allowed through, because the allocator is an opportunity-cost optimiser
-- layered on top of gates that already said yes — it is not a safety control,
-- and an allocator bug must not be indistinguishable from a market with no
-- opportunities. The gates that ARE safety controls (cost, structure, event,
-- conviction, staleness) all ran before it and none of them fails open.
--
-- 4. QUOTE MODE -> ON, with parity logging kept armed
--
-- The live day range, session volume and exchange VWAP now feed the breakout
-- conditions instead of a value up to 300 seconds stale. Parity logging stays
-- ON so the two sources keep being compared while the live one is in use —
-- `python -m tools.quote_parity` after a session, and if it reports a fault,
-- one UPDATE puts it back.
--
-- 5. WHAT STAYS IN SHADOW, AND WHY IT IS NOT A CAPABILITY BEING WITHHELD
--
--     RVS, MOM   swing screeners   n=1,471 @ -0.49% / n=1,090 @ +0.05%
--     VCE, RNG   intraday engines  E[R] -0.941 / -1.376, both p90 -1.00
--
-- All four remain fully integrated and running: VCE and RNG evaluate every
-- symbol every cycle (157 detections recorded), RVS and MOM screen every night
-- (258 and 193 rows in screener_performance). They are generating the evidence
-- that will settle whether they are retired or restored. What they do not get
-- is capital, which is Stage 6's conclusion rather than a pending decision.
-- Operator's call, 05-Aug-2026: continue shadowing until the sample decides.
-- ═══════════════════════════════════════════════════════════════════════════

UPDATE public.system_config SET value = '4',   updated_at = now() WHERE key = 'autonomy_phase';
UPDATE public.system_config SET value = '4.0', updated_at = now() WHERE key = 'intraday_autonomy_phase';

UPDATE public.strategy_config
   SET lifecycle = 'ACTIVE', updated_at = now()
 WHERE strategy IN ('PEAD', 'ACC');

UPDATE public.system_config SET value = 'true',  updated_at = now() WHERE key = 'alloc_live_intraday';
UPDATE public.system_config SET value = 'false', updated_at = now() WHERE key = 'alloc_live_swing';
UPDATE public.system_config SET value = 'true',  updated_at = now() WHERE key = 'intraday_quote_mode';

DO $$
DECLARE ph text; shadowed integer;
BEGIN
  SELECT value INTO ph FROM public.system_config WHERE key = 'autonomy_phase';
  SELECT count(*) INTO shadowed FROM public.strategy_config WHERE lifecycle = 'SHADOW';

  RAISE NOTICE '';
  RAISE NOTICE 'TradeOS v7 — autonomy phase %.', ph;
  RAISE NOTICE '  PEAD and ACC are ACTIVE and score into the composite.';
  RAISE NOTICE '  Allocator VETOES intraday entries. Swing follows after one session.';
  RAISE NOTICE '  Quote mode live; parity logging still armed so it self-checks.';
  RAISE NOTICE '  % swing engine(s) still shadowed on measured evidence.', shadowed;
  RAISE NOTICE '';
  RAISE NOTICE 'AFTER TOMORROW''S SESSION:';
  RAISE NOTICE '    python -m tools.allocator_report      does its output make sense?';
  RAISE NOTICE '    python -m tools.quote_parity          do the two feeds agree?';
  RAISE NOTICE '  Then, only if both read clean:';
  RAISE NOTICE '    UPDATE system_config SET value=''true'' WHERE key=''alloc_live_swing'';';
  RAISE NOTICE '';
  RAISE NOTICE 'Roll everything back with:  python -m tools.rollback --all-off';
END $$;
