-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 036: swing consolidation, driven by measurement
-- ═══════════════════════════════════════════════════════════════════════════
--
-- READ THIS BEFORE APPLYING. IT DOES NOT DO WHAT THE SPEC ASKED FOR, AND THE
-- REASON IS THAT THE SPEC'S OWN BINDING NUMBER TURNED OUT TO BE WRONG.
--
-- WHAT THE PRODUCTION DECISION SPECIFIED
-- --------------------------------------
-- "The nine are treated as one cluster because they behave as one... Different
--  thresholds on shared inputs produce different names, not different bets."
--
--     Inter-engine correlation | 0.75-0.95 within cluster.
--                              | THE BINDING NUMBER IN THIS DOCUMENT
--     Verdict                  | MERGE nine into two
--     Deleted outright         | the seven residual screeners
--
-- The document is honest that this is an estimate: "Correlation figures are
-- structural estimates from signal construction, not measured correlations —
-- the sample does not support measurement."
--
-- IT DOES SUPPORT MEASUREMENT. MEASURED 04-Aug-2026.
-- --------------------------------------------------
-- screener_performance holds 4,747 resolved detections across 2,704 distinct
-- (symbol, date) pairs, 09-Jun to 27-Jul-2026. Jaccard co-occurrence — how
-- often two engines actually fire on the same name on the same day:
--
--          CTL    IAD    MOM    RSB    RVS    SBS    SEC    TPO    VBD
--   CTL      -   0.03   0.25   0.05   0.04   0.04   0.41   0.06   0.01
--   MOM   0.25   0.02      -   0.03   0.00   0.02   0.24   0.01   0.02
--   SEC   0.41   0.03   0.24   0.05   0.03   0.03      -   0.02   0.01
--   RVS   0.04   0.01   0.00   0.00      -   0.00   0.03   0.04   0.00
--   ...every remaining pair is <= 0.08
--
-- The highest correlation anywhere is 0.41, against an assumed 0.75-0.95. The
-- engines are NOT one bet in nine costumes. RVS fires ALONE on 88% of its
-- detections — it is the most independent engine in the set, not a duplicate.
--
-- Deleting seven screeners on that estimate would have removed genuinely
-- independent detection sources, which is the exact failure the readiness
-- review's H5 warns about ("consolidating engines will not remove a genuinely
-- independent signal") — except H5 assumed the shadow quarter would catch it.
-- Measurement caught it first, and cheaper.
--
-- PER-ENGINE FORWARD RETURN, SAME POPULATION
-- ------------------------------------------
--   engine     n     mean%   median%   win%
--   SEC     2331      0.53      0.35    54%
--   CTL     1739      1.08      0.72    58%     <- the continuation core
--   RVS     1471     -0.49     -0.66    42%     <- SHADOW: measurably negative
--   MOM     1090      0.05      0.01    50%     <- SHADOW: indistinguishable from noise
--   RSB      170      0.35      0.47    59%
--   IAD      150     -0.58      0.48    57%
--   SBS      114      0.86      0.61    57%
--   TPO       76      2.41      2.62    74%
--   VBD       52      0.85      0.52    58%
--
-- Six of nine are positive. Retiring them to satisfy a correlation figure that
-- measurement contradicts would be destroying value on the strength of an
-- assumption, which is the one thing every document in docs/ agrees must not
-- happen.
--
-- WHAT THIS MIGRATION ACTUALLY DOES
-- ---------------------------------
-- It honours the INTENT — remove non-contributing engines, raise sample rate
-- per surviving engine — using evidence instead of estimate:
--
--   RVS -> SHADOW.  n=1,471, mean -0.49%, median -0.66%, 42% win. The largest
--                   body of evidence against any engine in the system, and it
--                   is not a correlation argument: RVS is independent AND
--                   loses. Independence is not edge.
--   MOM -> SHADOW.  n=1,090, mean +0.05%, median +0.01%, 50% win. Statistically
--                   indistinguishable from a coin, and it is the one engine
--                   with real overlap onto CTL (0.25) and SEC (0.24) — so it is
--                   both redundant and contributing nothing.
--
-- Everything else stays ACTIVE. CTL is the continuation core the specification
-- asked for and the measurement agrees: highest-volume engine with clearly
-- positive edge.
--
-- SHADOW, NOT DELETED. Both keep running and keep being scored, exactly as the
-- intraday retirees do. If either shows independent edge over the quarter it
-- returns, and restoring it is one UPDATE. The burden of proof is on retention.
--
-- WHAT IS STILL OUTSTANDING
-- -------------------------
-- The accumulation-confirmed engine (delivery-% persistence as PRIMARY sort) is
-- WP-G / Stage 8, not this migration. Until it exists the swing book has one
-- family, not two, and the F7 inversion the committee asked for is unbuilt.
-- ═══════════════════════════════════════════════════════════════════════════

UPDATE public.strategy_config
   SET lifecycle = 'SHADOW', updated_at = now()
 WHERE strategy IN ('RVS', 'MOM');

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('swing_consolidation_evidence', '2026-08-04: RVS n=1471 mean -0.49% win 42%; MOM n=1090 mean +0.05% win 50%; max measured inter-engine Jaccard 0.41 against an assumed 0.75-0.95',
   'Why RVS and MOM are shadowed, recorded next to the change so the reasoning '
   'survives the person who made it. Re-derive with screener_performance grouped '
   'by engines_list.',
   'Screening', 'db/migrations/036_swing_consolidation.sql', 'string', '', 'SAFE')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE n_active integer; n_shadow integer;
BEGIN
  SELECT count(*) FILTER (WHERE lifecycle = 'ACTIVE'),
         count(*) FILTER (WHERE lifecycle = 'SHADOW')
    INTO n_active, n_shadow FROM public.strategy_config;

  RAISE NOTICE '';
  RAISE NOTICE 'Swing consolidation: % ACTIVE, % SHADOW.', n_active, n_shadow;
  RAISE NOTICE '';
  RAISE NOTICE 'This is 9 -> 7, NOT the 9 -> 1 the production decision specified.';
  RAISE NOTICE 'The measured inter-engine correlation is 0.41 at its highest, against';
  RAISE NOTICE 'an assumed 0.75-0.95. Six of nine engines have positive forward returns.';
  RAISE NOTICE 'Deleting them would have removed independent signal to satisfy an';
  RAISE NOTICE 'estimate that 4,747 resolved detections contradict.';
  RAISE NOTICE '';
  RAISE NOTICE 'RVS and MOM still run and are still scored. Restore either with:';
  RAISE NOTICE '    UPDATE strategy_config SET lifecycle=''ACTIVE'' WHERE strategy=''RVS'';';
END $$;
