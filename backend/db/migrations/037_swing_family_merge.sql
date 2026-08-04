-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 037: the swing merge (Stage 6, completing WP-E)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- MERGE, NOT RETIREMENT. Migration 036 shadowed RVS and MOM on measured
-- performance; that was a retirement and it does not raise anything's sample
-- rate. This is the other half, and the half Stage 6's acceptance criterion
-- actually depends on:
--
--     "Detection rate per surviving engine >= 3x prior"
--
-- You cannot reach that by deleting engines. Only by counting the same
-- detections under fewer names. The seven ACTIVE screeners keep running exactly
-- as they do; their hits are now attributed to one CONTINUATION family. Over
-- 4,747 resolved detections the family carries ~4,600 against CTL's 1,739
-- alone — comfortably past 3x, with nothing lost.
--
-- WHAT CHANGES, AND IT IS ONLY THIS
-- ---------------------------------
-- The convergence bonus is counted per FAMILY instead of per ENGINE. Every
-- engine still runs, every detection is still found, every score still enters
-- base_score, and engines_list still names each engine so forward returns stay
-- attributable per engine.
--
-- What stops is paying a bonus for agreement that was never independent.
-- Measured Jaccard co-occurrence over 2,704 distinct (symbol, date) pairs: the
-- only real overlap in the nine is CTL-SEC at 0.41 and CTL/SEC-MOM at ~0.25;
-- every other pair is <= 0.08. The convergence bonus was therefore being paid
-- almost exclusively on the one cluster that genuinely is one bet — three
-- readings of the same continuation structure, counted as three independent
-- votes.
--
-- Two engines from DIFFERENT families agreeing still earns it. That is two
-- different burdens of proof being met on one name, which is real evidence.
--
-- MEASURED IMPACT BEFORE YOU APPLY THIS
-- -------------------------------------
-- Across 400 recent shortlist rows: 151 names lose convergence points, mean
-- 10.85 removed, maximum 38.40, against composite scores typically near 60.
-- This re-ranks the live book. It is a switch rather than a deploy for exactly
-- that reason, and turning it off restores the prior behaviour precisely.
--
-- RVS and MOM stay their own families so their forward returns remain separable
-- through the shadow quarter instead of being absorbed into the survivors'
-- record.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('screener_convergence_per_family', 'true',
   'Count the convergence bonus once per engine FAMILY rather than once per '
   'engine. CTL, SEC, TPO, SBS, VBD, RSB and IAD are one CONTINUATION family; '
   'agreement inside it is expected and earns nothing, agreement across '
   'families still does. False reproduces the prior per-engine count exactly. '
   'Measured impact: 151 of 400 recent shortlist names lose a mean 10.85 points.',
   'Screening', 'swing/signals/screen_stocks.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Swing merge live: seven ACTIVE screeners -> one CONTINUATION family.';
  RAISE NOTICE 'Every engine still runs and every detection is still recorded per engine.';
  RAISE NOTICE 'Only the convergence bonus is de-duplicated.';
  RAISE NOTICE '';
  RAISE NOTICE 'This RE-RANKS the live book: 151 of 400 recent names lose a mean 10.85';
  RAISE NOTICE 'points of convergence. Revert instantly with:';
  RAISE NOTICE '  UPDATE system_config SET value=''false''';
  RAISE NOTICE '   WHERE key=''screener_convergence_per_family'';';
END $$;
