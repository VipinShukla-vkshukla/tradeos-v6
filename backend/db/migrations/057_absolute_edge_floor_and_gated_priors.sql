-- 10-Aug-2026. Two switches for the pair of defects that let the INTRADAY book
-- take a trade it had itself measured as losing.
--
-- WHAT WAS OBSERVED, 10-Aug-2026 (live, PAPER book, alloc_live_intraday=true):
--
--   allocation_decisions, INTRADAY, by bucket:
--     DECLINE STRONG  141 rows, mean edge -1.0937
--     TAKE    STRONG    1 row,  mean edge -1.0935
--     DECLINE WEAK    118 rows, mean edge -0.1854
--     TAKE    WEAK      2 rows, mean edge -0.5458
--
--   The same book on 07-Aug: TAKE 50 rows at mean edge +0.1705.
--
--   13:12:31  allocator: 1 proposal scored, 1 to take -- bar -1.09359
--   13:12:31  PAPER POSITION opened DEVYANI 65 @ 133.46 [INTRADAY/SDN]
--   13:14:10  CLOSED DEVYANI @ 134.11  -0.49%  (-0.813R)  SETUP_INVALIDATED
--
-- DEVYANI was scored at edge -1.0935 against a bar of -1.09359 and cleared it
-- by 0.00009. Across the STRONG bucket the taken and declined populations
-- differed by two ten-thousandths of an R. `edge` is expected R NET of the
-- round trip, so every one of those 262 proposals was a trade the scorer had
-- already measured as losing money. The allocator was not choosing between
-- trades; it was ranking losses against each other and taking the least bad.
--
-- CAUSE 1 -- the bar is purely RELATIVE. hurdle() is a percentile of
-- allocation_decisions.edge, which answers "better than what else is
-- arriving?" and never "worth doing at all?". A percentile of an all-negative
-- population is negative, and the bar then DECAYS toward that base as the
-- session runs out (time_mult -> 1.0), so willingness to take a measured loser
-- RISES with the certainty that it is one. Correct for a positive population
-- (an unspent slot earns nothing at 14:45); inverted for a negative one,
-- because the alternative to a losing trade is not zero, it is better than
-- zero -- you keep the round trip.
--
-- CAUSE 2 -- the prior was built from REFUSED trades. scoring.intraday_priors()
-- reads every intraday_setups row with a resolved outcome, including
-- BLOCKED_STRUCTURE / REJECTED_COST / VETOED_AI / BELOW_CONVICTION. On 10-Aug
-- that was 127 rows of which 15 were TAKEN -- so the prior pricing each new
-- candidate was ~88% trades the system had deliberately refused. That inverts
-- the learning loop: every gate that WORKS pushes more bad outcomes into the
-- prior, lowering every future edge, lowering the bar (a percentile of that
-- same scored population), admitting worse trades. The better the gates get,
-- the more negative the system believes itself to be.
--
-- The two fixes are DELIBERATELY PAIRED. The floor alone, applied to 10-Aug's
-- population, refuses all 262 proposals and blacks out the book -- correctly,
-- but with nothing left to trade. The prior fix is what puts edge back above
-- zero so the floor has something to let through. Neither should be enabled
-- without the other.
--
-- NOT CHANGED HERE: alloc_hurdle_dedup_intraday stays false. Migration 054
-- measured it (n=346 raw / 35 deduplicated, 9.9x inflation) and found it moves
-- the intraday bar STRICTER, not looser -- p75 -1.329R -> -0.900R -- because
-- the rows that repeat most on intraday are near-miss setups re-logged every
-- 15s while they hover without triggering, which inflates the BAD tail. That
-- verdict stands. Both of those p75 values are negative and therefore clamped
-- to the floor anyway, so the switch cannot affect the negative regime at all
-- once this migration is applied. Re-measure it only AFTER the gated prior has
-- moved the edge population positive -- that is precisely the "engine mix
-- changed materially" trigger migration 054 names for re-measurement.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('alloc_edge_absolute_floor', '0.0',
   'The lowest value allocation/hurdle.py::hurdle() may ever return for a bar '
   'built from a real arrival population. `edge` is expected R net of the '
   'round trip, so 0.0 means "never take a proposal the scorer has measured '
   'as losing money", charged once -- sign(edge) == sign(e_r) because '
   'edge = e_r / max(hold_days, 0.5) and that divisor is always positive. '
   'Applied to the FINAL bar, after the time and scarcity multipliers, NOT to '
   'the percentile base -- flooring the base would compound with those '
   'multipliers and demand a proposal beat its costs by the full percentile on '
   'top, which is the second hidden cost charge _empirical_base warns against. '
   'When the population is healthy and positive this never binds and the '
   'percentile does all the selecting. COLD STARTS ARE EXEMPT: an allocator '
   'with no history must stay indistinguishable from no allocator at all, and '
   '"no opinion" is not the same claim as "measured bad". Raising this above '
   '0.0 demands a positive expected R and will reduce trade count; lowering it '
   'below 0.0 re-opens the 10-Aug DEVYANI path.',
   'Master controls', 'allocation/hurdle.py', 'float', '0.0', 'HIGH'),
  ('priors_intraday_taken_only', 'true',
   'When true, scoring.intraday_priors() builds each engine''s R prior from '
   'intraday_setups rows with cost_verdict = TAKEN -- the detections that '
   'cleared every safety gate -- instead of from every resolved detection '
   'including the ones those gates threw out. The prior answers "if I take a '
   'trade from this engine, what R should I expect", so the sample must be '
   'trades this system would take. A setup later refused by the ALLOCATOR '
   'still counts: its TAKEN row remains (a verdict change writes a fresh row), '
   'and allocator refusal is opportunity cost, not a safety veto. Refused '
   'detections are NOT deleted -- they remain the rarest asset this '
   'architecture has, for measuring what standing down cost; they are simply '
   'not the population that prices a candidate which already passed every gate '
   'they failed. PER-ENGINE FALLBACK: an engine with fewer than '
   'priors_min_sample_intraday TAKEN rows falls back to its full detection '
   'history rather than dropping to NEUTRAL, and the Prior''s note records '
   'which sample it used so no number is borrowed silently. Setting this false '
   'restores the pre-10-Aug behaviour and re-creates the inverted learning '
   'loop described in this migration''s header.',
   'Master controls', 'allocation/scoring.py', 'bool', 'true', 'HIGH')
ON CONFLICT (key) DO NOTHING;
