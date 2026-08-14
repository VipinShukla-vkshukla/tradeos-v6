-- 15-Aug-2026. Two defects in analysis/risk_model.py, the module that writes
-- signal_output_daily.planned_target on the LIVE swing book.
--
-- NUMBERED 079, NOT 077. Migration 077 (single_daemon_lock) is on the unmerged
-- branch fix/single-daemon-lease and 078 landed on main. Same reasoning as 078:
-- two migrations sharing a number the moment both land is not worth the tidier
-- sequence. These are independent of both and may be applied in any order.
--
-- 1. REGIME MOVED R. regime_k scaled the STOP (risk_model.py:161) and not the
--    TARGET (:188). Both are ATR distances -- the two sides of one ratio -- so a
--    knob whose stated purpose is volatility was silently a knob on
--    reward-per-unit-risk:
--
--        planned R = target_atr_mult / (stop_atr_mult * regime_k) = 3.0/(1.5*k)
--        TRENDING 2.1050  RISK ON 2.0000  NEUTRAL 1.9048  RISK OFF 1.6000
--
--    R therefore SHRANK as conditions worsened -- more risk per share for
--    identical reward, exactly when the market is least likely to pay for it.
--    The 2R design point was reachable only in RISK ON, which this book has
--    never traded: all 1000 plans in the 28-Jul -> 13-Aug window read NEUTRAL,
--    so 1.9048R was not drift and not a distribution, it was that one constant.
--
--    The switch applies k to the target on the ATR branch ONLY. A structural
--    stop is a PRICE, so k never scaled its risk; scaling only its target would
--    raise R with no offsetting risk change -- a free 5% on 310 of 995 plans.
--
-- 2. THE PLANNER HAD NO COST MODEL. risk_model.py imported dataclasses and
--    nothing else, while every other gate in the system prices its own friction.
--    It now sizes each plan by the production rule (risk_pct_per_trade,
--    max_position_pct -- the same two keys portfolio_constraints reads, not a
--    second copy), prices that clip's round trip, and reports friction_r and
--    required_rr on EVERY plan whether or not the floor below is armed.
--
--    THE BASIS IS LEDGER, NOT GATE. entry_leg+exit_leg (statutory only), not
--    round_trip (which adds 5 bps slippage per leg). The two differ by a
--    constant +0.100pp of position: 1.10-1.17x on CNC clips, 1.94x on MIS.
--    planned R is ultimately compared against REALISED R by expectancy_ledger,
--    weekly_review and every prior built from closed_positions, and all of those
--    price friction statutorily because slippage is already inside the fill
--    price on both books. Charging it again here would be a double count
--    against the very number this floor exists to protect.
--
-- BOTH SHIP INERT, in code as well as here. Arming either changes what the
-- account does with money and is a separate decision on separate evidence --
-- unlike 078, where a guard inert until a migration runs is no guard at all.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('risk_regime_scales_target', 'false',
   'When true, analysis/risk_model.py scales the TARGET distance by the same '
   'regime_k it already applies to the ATR stop, making planned R equal to '
   'target_atr_mult/stop_atr_mult (2.0) in every regime instead of '
   '3.0/(1.5*k). Applied on the ATR stop branch only -- a structural stop is a '
   'price regime_k never scaled, so scaling its target would raise R without '
   'raising risk. FALSE reproduces today''s behaviour exactly, including the '
   '1.9048R NEUTRAL constant. Turning it on raises planned R by 1/k on every '
   'ATR-stop plan (+5.0% in NEUTRAL) and changes no stop. See '
   'tests/test_planner_regime_and_cost.py.',
   'Risk', 'analysis/risk_model.py', 'bool', 'false', 'HIGH'),

  ('risk_min_planned_r_enabled', 'false',
   'When true, analysis/risk_model.py rejects a plan whose planned R is below '
   'the R at which it breaks even against its OWN statutory round trip, at its '
   'own stop and its own clip: required = (1 - h + friction_R)/h. DEFAULTS OFF '
   'because it refuses trades -- on the 28-Jul to 13-Aug window it would have '
   'refused 185 of 784 fundable plans (23.6%) unfixed, and 71 (9.1%) with '
   'risk_regime_scales_target also on. A plan that cannot be sized (no clip) or whose charge schedule '
   'cannot be read is NEVER refused by this floor: that is absent evidence, not '
   'measured-bad, and refusing to fund a share is portfolio_constraints'' job '
   'with its own reason string. See tests/test_planner_regime_and_cost.py.',
   'Risk', 'analysis/risk_model.py', 'bool', 'false', 'HIGH'),

  ('risk_plan_hit_rate', '0.40',
   'The hit rate the planner takes its break-even at, for risk_min_planned_r_'
   'enabled. 0.40 is the swing book''s design hit rate. NOTE WHAT THE IDENTITY '
   'ASSUMES: winners pay exactly the planned R and losers exactly 1R. Of the 10 '
   'closed swing trades with a full planned geometry, ONE reached its planned '
   'target and NONE reached its planned stop -- every other exit was resolved '
   'by the ladder in between (FINDINGS F-2). This floor is a planning '
   'discipline, not a forecast of realised expectancy.',
   'Risk', 'analysis/risk_model.py', 'float', '0.40', 'MEDIUM'),

  ('risk_plan_r_margin', '0.0',
   'Extra R demanded ABOVE break-even by risk_min_planned_r_enabled. 0.0 means '
   'the bar is break-even exactly. Raising it is how the floor is armed '
   'conservatively without moving the hit-rate assumption, which is a claim '
   'about the world rather than a choice about margin of safety.',
   'Risk', 'analysis/risk_model.py', 'float', '0.0', 'MEDIUM'),

  ('risk_plan_product', 'CNC',
   'Product the planner prices friction against. The swing book is delivery, '
   'and CNC is 6-9x dearer than MIS per rupee of position: 0.1% STT on BOTH '
   'legs against 0.025% on the sell only, plus the flat Rs 15.04 DP fee, which '
   'on a Rs 2,000 clip is 0.75% by itself. Pricing a swing plan as MIS '
   'understates its friction by roughly an order of magnitude.',
   'Risk', 'analysis/risk_model.py', 'string', 'CNC', 'MEDIUM'),

  ('risk_plan_capital', '0',
   'Capital the planner sizes a plan''s clip against, to price its friction. '
   '0 means derive it from config.capital_for(''SWING''), which is the whole '
   'account while the intraday book is PAPER. Set explicitly only to plan '
   'against a figure other than the live sleeve; it does not affect what is '
   'actually bought, which portfolio_constraints sizes at entry.',
   'Risk', 'analysis/risk_model.py', 'float', '0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
