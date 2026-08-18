-- 16-Aug-2026. Four config changes, on evidence, plus one that was already made.
--
-- NUMBERED 083, NOT 081, for the same reason 082 above is not 080 -- see its
-- header. NOT APPLIED as of the 18-Aug merge onto main: this file changes
-- which live engines trade (VWR/ORB shadow) and the ledger entry below calls
-- it a "provisional stand-down, revisit on sessions scored entirely under the
-- new guard" -- an operator decision, not a merge decision.
--
-- BASELINE RECORDED BEFORE ANY OF THIS, so the comparison exists. Last five
-- sessions (10, 11, 12, 13, 14-Aug-2026), 6,179 detections:
--
--   BLOCKED_SHORTS_MARKET  1343  21.7%     VETOED_AI              589   9.5%
--   REJECTED_COST           847  13.7%     BLOCKED_STRUCTURE      418   6.8%
--   TAKEN                   816  13.2%     BLOCKED_SHORTABILITY   253   4.1%
--   BELOW_CONVICTION        809  13.1%     BLOCKED_SHORTS_OFF     138   2.2%
--   ALLOCATOR_DECLINED      748  12.1%     BLOCKED_REENTRY         66   1.1%
--                                          BLOCKED_EVENT           61   1.0%
--                                          BLOCKED_CROSS_FRAMEWORK 53   0.9%
--                                          BLOCKED_PAPER_CAPACITY  24   0.4%
--                                          BLOCKED_ENTRY_RESERVED  14   0.2%
--
--   entries per session: 14-Aug 334 - 13-Aug 93 - 12-Aug 108 - 11-Aug 4 - 10-Aug 277
--   detections/taken by engine: ORB 1189/572 - SDN 3837/104 - PBK 334/23 -
--     VWR 279/40 - VCE 230/71 - RNG 160/0 - PDL 98/0 - GAP 51/6 - GDB 1/0
--
-- READ THAT LAST LINE BEFORE APPLYING. ORB is 572 of 816 entries (70.1%) and
-- VWR another 40 (4.9%). Shadowing both removes THREE QUARTERS of the intraday
-- book's entries. The book is PAPER, so this costs no money; it costs
-- detections-that-become-trades, and the comparison above is how that is
-- measured rather than assumed.
--
-- ── 1. VWR -> SHADOW. Settled. ───────────────────────────────────────────────
--
-- 307 detections at gross -0.345R +/- 0.071 SE. It survives every bound that
-- was put to it: -4.9 SE from zero, and an OPTIMISTIC 2 SE upper limit of
-- -0.199 -- still negative. There is no reading of this sample under which VWR
-- is a positive-expectancy engine. SHADOW, not RETIRED: it keeps evaluating and
-- keeps being scored, because retiring an engine destroys the evidence needed
-- to decide whether retiring it was right.
--
-- ── 2. ORB -> SHADOW. NOT settled, and the reason it differs is recorded. ────
--
-- 119 detections at gross -0.241R +/- 0.100 SE, -2.4 SE from zero. Its
-- optimistic 2 SE upper limit is -0.010 -- it survives the bound BY 0.01R, one
-- hundredth of an R, which is inside the width of every measurement error this
-- book has. And ORB is the MOST CONTAMINATED engine in the population at 11.8%:
-- the highest share of rows carrying the F-27 mechanism-A defect that migration
-- 080 stops, meaning the highest share of outcomes frozen at an intra-session
-- price rather than scored against the whole session.
--
-- So the two engines are being given the same state for materially different
-- reasons, and the difference must not be lost by the state looking identical:
--
--     VWR  answered NO with room to spare, on clean-enough data
--     ORB  answered NO by 0.01R, on the dirtiest data in the book
--
-- REVISIT ORB ON CLEAN DATA -- sessions scored entirely after migration 080,
-- identifiable by scored_through landing at the session close. This is a
-- provisional stand-down, not a verdict. It is also why ORB is not RETIRED:
-- 70% of the book's entries are being withdrawn on a 0.01R margin, and the
-- shadow record is the only thing that can reverse it.
--
-- ── 3. THE CONVICTION FLOOR IS FLATTENED TO ITS BASE. ────────────────────────
--
-- engine._confidence_floor() returns base + (scarce - base) * budget_used, so
-- the bar RISES from 0.55 to 0.80 as the day's entry budget is consumed. The
-- premise is that a scarcer slot should be spent on a more convinced setup.
-- The book says the opposite: gross R falls MONOTONICALLY across the top four
-- confidence bands, -0.097 -> -0.400. Higher conviction is not merely
-- uninformative here, it points the wrong way -- so a floor that rises is a
-- filter that spends the last slots of the day on the worst population.
--
-- Flattened by setting `scarce` EQUAL TO `base` rather than by a new switch:
-- the linear term becomes exactly zero, no code changes, and the two keys stay
-- in place so re-arming the ramp is one UPDATE once there is evidence for it.
-- 0.55 is the CURRENT base, unchanged -- this removes a slope, it does not
-- lower a bar. BELOW_CONVICTION was 809 of 6,179 rows (13.1%) at the baseline.
--
-- ── 4. risk_regime_scales_target -> TRUE. This one touches the LIVE book. ────
--
-- Migration 079 shipped it inert and said arming it "changes what the account
-- does with money and is a separate decision on separate evidence". This is
-- that decision. regime_k scaled the ATR STOP and not the TARGET, so planned
-- R = 3.0/(1.5*k) SHRANK as conditions worsened -- more risk per share for
-- identical reward, exactly when the market is least likely to pay. Armed,
-- planned R is target_atr_mult/stop_atr_mult = 2.0 in every regime. In NEUTRAL
-- -- which is the only regime all 1000 plans in the 28-Jul -> 13-Aug window
-- have ever read -- that is 1.9048R -> 2.0R, +5.0%, on ATR-stop plans only.
-- No stop moves. Structural-stop plans are untouched.
--
-- MIGRATION 079 IS NOT APPLIED ON THIS BOOK. All six of its keys are absent and
-- the code runs on its in-code defaults, so behaviour today is identical to 079
-- having been applied and left inert. This migration therefore INSERTS the one
-- key it is arming rather than updating it. The other five stay absent and
-- inert; `risk_min_planned_r_enabled` in particular defaults FALSE in code, so
-- the plan-refusal floor stays off and this change cannot refuse a trade. 079
-- uses ON CONFLICT DO NOTHING, so applying it later will NOT reset this to
-- false.
--
-- ── 5. overlay_liquidity_enabled WAS ALREADY TRUE. Nothing to arm. ───────────
--
-- Recorded because "we armed it" and "it was already armed" are different
-- facts, and only one of them is true. system_config held 'true' before this
-- session began; migration 040 inserted it that way on 05-Aug. The UPSERT
-- below is a no-op that makes the intended state explicit rather than assumed.

-- 1 + 2. Engine lifecycle. These keys already exist and are ACTIVE, so UPDATE.
UPDATE public.system_config
   SET value = 'SHADOW',
       updated_at = now()
 WHERE key IN ('intraday_engine_vwr_lifecycle', 'intraday_engine_orb_lifecycle');

-- 3. The floor stops rising. base is unchanged at 0.55; scarce joins it.
UPDATE public.system_config
   SET value = '0.55',
       description = 'Upper end of the intraday conviction floor ramp. SET EQUAL '
                     'TO intraday_min_confidence (0.55) on 16-Aug-2026, which '
                     'makes engine._confidence_floor() flat: gross R falls '
                     'monotonically across the top four confidence bands '
                     '(-0.097 -> -0.400), so a floor that rises with budget '
                     'consumption spends the last slots of the day on the worst '
                     'population. Raise it above the base only on evidence that '
                     'conviction has started to order outcomes the right way.',
       updated_at = now()
 WHERE key = 'intraday_min_confidence_scarce';

-- 4. The one live-book change in this migration.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('risk_regime_scales_target', 'true',
   'When true, analysis/risk_model.py scales the TARGET distance by the same '
   'regime_k it already applies to the ATR stop, making planned R equal to '
   'target_atr_mult/stop_atr_mult (2.0) in every regime instead of '
   '3.0/(1.5*k). ARMED 16-Aug-2026: unarmed, planned R SHRANK as conditions '
   'worsened -- more risk per share for identical reward, exactly when the '
   'market is least likely to pay for it. Applied on the ATR stop branch only; '
   'a structural stop is a price regime_k never scaled. Effect in NEUTRAL, the '
   'only regime this book has traded: 1.9048R -> 2.0R, +5.0%, no stop moves. '
   'Migration 079 is the origin and is otherwise unapplied here.',
   'Risk', 'analysis/risk_model.py', 'bool', 'false', 'HIGH')
ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = now();

-- 5. Already true since migration 040. Stated, not changed.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('overlay_liquidity_enabled', 'true',
   'Structural liquidity overlay in analysis/overlays.py. Already TRUE before '
   '16-Aug-2026; this row makes the intended state explicit.',
   'Signals', 'analysis/overlays.py', 'bool', 'false', 'CRITICAL')
ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = now();
