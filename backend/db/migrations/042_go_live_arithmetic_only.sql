-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 042: the switches that are safe with zero evidence
-- ═══════════════════════════════════════════════════════════════════════════
--
-- The operator asked to make Phase 4 fully live, no shadow work, "unless you
-- disagree." This migration is the part with no disagreement: switches whose
-- effect is pure arithmetic risk-reduction, never a prediction, and therefore
-- never in need of a shadow quarter to earn trust.
--
-- What is NOT here, and why, is recorded in docs/6_IMPLEMENTATION_STATUS.md
-- section "05-Aug-2026 — the go-live decision". Briefly: the allocator has
-- recorded zero decisions, PEAD/ACC have zero detections, and RVS/MOM/VCE/RNG
-- are shadowed because they are MEASURED to lose money or be flat — not
-- because they are new. None of those four questions is answered by more code;
-- they are answered by more data, which this migration does not manufacture.
--
-- 1. exit_runner_cap_enforced -> true
--    Zero positions have ever been marked a runner since migration 031 added
--    the column (checked live: 0 open, 0 closed). Enabling the cap today
--    changes nothing until the day two positions are simultaneously past
--    target and want to run — at which point it caps at 2 rather than letting
--    a third open-profit bet stack. Pure downside protection, zero live effect
--    today, so it needs no evidence to turn on.
--
-- 2. sizing_max_cost_r — registered, LEFT OFF, with the finding attached
--    This one is NOT flipped, and the reason is a real finding rather than
--    caution for its own sake. Measured 04-Aug-2026 across this account's own
--    CNC clip sizes:
--
--        clip              friction, in R
--        Rs 0-2,500        2.363
--        Rs 2,500-5,000    1.046
--        Rs 10,000-25,000  0.605   <- the account's LARGEST current clip band
--
--    Even a lenient cap of 0.5R would refuse essentially every CNC trade at
--    today's position sizes, because the flat Rs 15.04 DP fee dominates a
--    Rs 4,000 clip. Turning this on as-is would not fix the swing book, it
--    would nearly halt it. The actual fix per Stage 2 deliverable 4 is fewer,
--    larger positions within an unchanged risk budget — a position-sizing
--    change this migration does not make, because it was never asked for and
--    changes what capital does on every trade, not just the losing ones.
--    Registered here so it is no longer a silent default (cfg_float was
--    returning 0.0 from a Python default with no row describing it, which is
--    the exact failure class CLAUDE.md calls the dominant one in this project)
--    and so it is visible and tunable from the Control Room going forward.
-- ═══════════════════════════════════════════════════════════════════════════

UPDATE public.system_config
   SET value = 'true', updated_at = now()
 WHERE key = 'exit_runner_cap_enforced';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('sizing_max_cost_r', '0',
   'Refuse a trade whose round-trip friction exceeds this multiple of its own '
   'risk budget. 0 disables the gate (the shipped default, unchanged). LEFT AT '
   '0 on 05-Aug-2026 despite the go-live pass: measured CNC friction at this '
   'account''s clip sizes is 0.605-2.363R, so any cap below ~0.7 would refuse '
   'nearly every swing trade the account currently takes. The fix is larger '
   'clips (Stage 2 deliverable 4), not this gate alone — raise position size '
   'caps first if this is to be turned on. See migration 042.',
   'Portfolio & sizing', 'analysis/portfolio_constraints.py', 'float', '0', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'exit_runner_cap_enforced -> true. No live effect today: 0 runners exist.';
  RAISE NOTICE 'sizing_max_cost_r registered and left OFF — see the migration header,';
  RAISE NOTICE 'this account''s own clip sizes would fail any cap worth setting.';
END $$;


-- ── Allocator outcome scoring: the producer the promotion gate needs ──────
--
-- allocation_decisions.outcome_r is what tools/allocator_report counts a
-- DISAGREEMENT on. Migration 041 created the column and nothing wrote it, so
-- the allocator could have shadowed for a year while the scorecard reported
-- zero scored disagreements — identical in shape to the 1,711 plan outcomes
-- that sat NULL because final_snapshot wrote them and nothing filled them in.
-- allocation/outcomes.py is the producer; pipeline step 28b runs it nightly.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('alloc_counterfactual_hold_days', '15',
   'Sessions a refused proposal is walked forward before its counterfactual is '
   'settled. Matches the swing plan resolver so a TAKE and a DECLINE on the '
   'same name are measured over the same horizon.',
   'Scoring', 'allocation/outcomes.py', 'int', '15', 'TUNING'),

  ('alloc_shortfall_r', '0.05',
   'Haircut applied to a POSITIVE counterfactual, in R. A declined proposal is '
   'scored as if filled at the price it was refused at, with no slippage and no '
   'partial — which systematically flatters whichever policy refuses more, and '
   'that is the allocator. THIS IS AN ASSUMPTION, NOT A MEASUREMENT: M1 in the '
   'readiness review nominated the order path for real shortfall instrumentation '
   'and Phase 4 did not build it. Derived from the cost model''s own 5bps '
   'slippage on a ~1% risk trade and rounded UP, because an optimistic haircut '
   'is what promotes the allocator wrongly. Never applied to a losing '
   'counterfactual, which would make a loss look better.',
   'Scoring', 'allocation/outcomes.py', 'float', '0.05', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;
