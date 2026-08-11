-- 11-Aug-2026. Answers the review's "identify the right setup, apply the
-- right strategy" question at the ENGINE-SELECTION level: no engine was
-- ever chosen or weighted per market regime -- regime_bucket() gated the
-- allocator's BAR (hurdle.py), never which engine's opinion to trust more.
--
-- allocation/scoring.py::regime_fit_multiplier() classifies each intraday
-- engine as MOMENTUM (needs a trend to break into: ORB, GAP, PDL, PBK,
-- VCE, SDN) or MEAN_REVERSION (needs the absence of one: RNG, VWR) from
-- the engine's OWN module docstring, and nudges its prior's mean_r by a
-- bounded amount depending on the live market_context state.
--
-- SHIPPED AT WEIGHT 0.0 -- AN EXACT NO-OP -- ON PURPOSE, mirroring
-- rank_weight_tier and rank_weight_conviction's own precedent
-- (entry_ranking.py): an unmeasured component deciding where capital goes
-- is unpriced risk, and this classification is structural, not measured.
-- hurdle.py's own docstring already states the project's position on
-- exactly this question: "Per-regime fitting is Phase 5 and is gated on
-- years of data, not on cleverness" -- and that caution has already been
-- paid for twice (the STRONG hurdle bucket's two separate self-reference
-- failures, both in this project's own history).
--
-- Migration 068 (same session) adds regime_at_detection so
-- allocation.scoring.regime_fit_report() can eventually measure whether
-- this classification actually holds -- confirming it if MOMENTUM
-- outperforms in RISK_ON and MEAN_REVERSION outperforms in NEUTRAL/
-- CAUTION, the same tercile-style evidence bar rank_weight_screener and
-- rank_weight_rr were both raised on. Raise this weight only on that
-- evidence, not on the theory alone.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_regime_fit_weight', '0.0',
   'Weight of the momentum/mean-reversion regime-fit nudge on each intraday '
   'engine''s prior (allocation.scoring.regime_fit_multiplier()). 0.0 = '
   'exact no-op, matching rank_weight_tier/rank_weight_conviction''s own '
   'pending-validation precedent -- this classification is structural '
   '(from each engine''s own docstring), not yet measured. Raise only on '
   'evidence from python -m allocation.scoring --regime-fit, once '
   'regime_at_detection (migration 068) has accumulated enough TAKEN rows.',
   'Scoring', 'allocation/scoring.py', 'float', '0.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
