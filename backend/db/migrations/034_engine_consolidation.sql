-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 034: engine consolidation, shadow phase (Stage 6)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE MEASUREMENT THAT AGREES WITH THE ARCHITECTURE
-- --------------------------------------------------
-- Stage 6 nominates VCE and RNG for retirement on structural grounds, written
-- before anyone had per-engine expected R. Stage 4 then produced it, from all
-- 595 resolved detections — the full field, not the traded subset:
--
--     engine   E[R]      n     p90
--     GAP      -0.019   103   +1.85
--     ORB      -0.421    30   +0.91
--     VWR      -0.544   154   +1.70
--     PBK      -0.663    60   +0.79
--     VCE      -0.941    75   -1.00      <- nominated
--     PDL      -1.008    97   +0.30
--     RNG      -1.376    76   -1.00      <- nominated
--     ALL      -0.691   595
--
-- The two engines the architecture named are measurably the two worst, and both
-- have a p90 of -1.00: nine detections in ten lose a full R or more. They are
-- not marginal, and the agreement between a structural argument and an
-- independent measurement is the strongest evidence this system has produced.
--
-- SHADOW, NOT OFF. THE DISTINCTION IS THE WHOLE STAGE.
-- ----------------------------------------------------
-- Until migration 034 an intraday engine had two states: enabled, or not.
-- Turning one off stopped it evaluating, which stopped it recording, which
-- stopped outcomes.resolve_day scoring it. Retiring an engine that way destroys
-- the evidence needed to judge whether retiring it was right — the quarter
-- passes silently and "no data" gets read as "no edge".
--
-- registry.py now carries ACTIVE / SHADOW / RETIRED, matching what the swing
-- engine registry has always had. SHADOW engines evaluate, their detections are
-- written with verdict 'SHADOW' and qty 0, outcomes resolve them, and they can
-- never be chosen as `best` no matter how confident they are.
--
-- They also cannot lift another engine's confidence through corroboration. A
-- retiree agreeing with a survivor is not independent evidence — expressing the
-- same bet is precisely why it is being retired — and counting it would
-- re-admit the engine through the back door.
--
-- THE BURDEN OF PROOF IS ON RETENTION
-- -----------------------------------
-- One full quarter. If a shadowed engine demonstrates independent edge over
-- that window it returns; silence is not a reprieve. Nothing is deleted, and
-- restoring either engine is one UPDATE.
--
-- WHAT IS DELIBERATELY NOT DONE HERE
-- ----------------------------------
-- The specification also calls for GAP and PDL to be absorbed into the ORB
-- family as day-type conditions, PBK into VWR as a condition, and seven swing
-- screeners merged into one continuation engine with unified thresholds. Those
-- are code merges that change what the surviving engines DETECT, and they rest
-- on correlation figures the readiness review (H5) states are structural
-- estimates rather than measured values, "because the sample does not support
-- measurement".
--
-- Shadowing first is what produces that sample. Merging on an unmeasured
-- premise and shadowing to test it are contradictory orders of operation, and
-- the architecture picked the second: retirement is shadowed for a quarter,
-- and the burden of proof is on retention. The merges follow the evidence.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_engine_vce_lifecycle', 'SHADOW',
   'Squeeze/compression engine. SHADOW from 04-Aug-2026: E[R] -0.941 across 75 '
   'resolved detections, p90 -1.00. Still evaluates and is still scored; simply '
   'receives no capital. ACTIVE restores it.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('intraday_engine_rng_lifecycle', 'SHADOW',
   'Range-fade engine. SHADOW from 04-Aug-2026: E[R] -1.376 across 76 resolved '
   'detections, the worst of the seven, p90 -1.00. Still evaluates and is still '
   'scored. ACTIVE restores it.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('intraday_engine_orb_lifecycle', 'ACTIVE',
   'Opening-range breakout. Survivor, and the base of the ORB family that GAP '
   'and PDL are intended to join as day-type conditions.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('intraday_engine_vwr_lifecycle', 'ACTIVE',
   'VWAP reclaim — institutional benchmark defence. Survivor, and the base of '
   'the family PBK is intended to join as a condition.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('intraday_engine_gap_lifecycle', 'ACTIVE',
   'Gap-and-go. Best measured engine at E[R] -0.019 (n=103) — essentially flat '
   'rather than losing. Intended to become a day-type condition within the ORB '
   'family; ACTIVE until that merge is justified by measurement.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('intraday_engine_pdl_lifecycle', 'ACTIVE',
   'Previous-day level retest. E[R] -1.008 (n=97). Intended to become a '
   'reference condition within the ORB family.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('intraday_engine_pbk_lifecycle', 'ACTIVE',
   'Trend pullback. E[R] -0.663 (n=60). Intended to become a condition within '
   'the VWAP-defence family.',
   'Intraday', 'intraday/strategies/registry.py', 'string', 'ACTIVE', 'CRITICAL'),

  ('engine_shadow_quarter_start', '2026-08-04',
   'When the current shadow quarter began. Retirement decisions are denominated '
   'in a full quarter of resolved detections, not in elapsed sessions — an '
   'engine that detected nothing has proved nothing.',
   'Brain / self-tuning', 'tools/weekly_review.py', 'string', '', 'SAFE')
ON CONFLICT (key) DO NOTHING;


-- ── Keep intraday_strategy_config truthful about it ───────────────────────
-- The Control Room reads this table. A lifecycle shown as ACTIVE while config
-- says SHADOW is exactly the kind of quiet disagreement that makes an operator
-- distrust the panel.
UPDATE public.intraday_strategy_config
   SET lifecycle = 'SHADOW', updated_at = now()
 WHERE strategy IN ('VCE', 'RNG');


DO $$
DECLARE n_shadow integer;
BEGIN
  SELECT count(*) INTO n_shadow
    FROM public.intraday_strategy_config WHERE lifecycle = 'SHADOW';

  RAISE NOTICE '';
  RAISE NOTICE 'Engine consolidation, shadow phase. % engine(s) shadowed.', n_shadow;
  RAISE NOTICE '';
  RAISE NOTICE 'VCE and RNG still run and are still scored. They receive no capital';
  RAISE NOTICE 'and cannot corroborate another engine. Nothing was deleted.';
  RAISE NOTICE '';
  RAISE NOTICE 'Review after one full quarter of resolved detections:';
  RAISE NOTICE '    cd backend && python -m allocation.scoring';
  RAISE NOTICE 'Restore either with:';
  RAISE NOTICE '    UPDATE system_config SET value=''ACTIVE''';
  RAISE NOTICE '     WHERE key=''intraday_engine_rng_lifecycle'';';
END $$;
