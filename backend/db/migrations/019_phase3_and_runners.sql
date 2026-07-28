-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 019: Phase 3 execution + runner logic
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THIS MIGRATION TURNS ON LIVE ORDER PLACEMENT.
-- After it runs, the system can sell your holdings without asking. Read the
-- rails below before applying it, and check that DRY_RUN is False in .env —
-- it is a fourth independent gate and no config value here overrides it.
--
-- WHAT BECOMES AUTOMATIC
--   exits and partial books, on BOTH frameworks
--
-- WHAT STAYS MANUAL, DELIBERATELY
--   entries. Committing new capital is the highest-variance decision the system
--   makes, and an exit is bounded by what you already hold while an entry is
--   bounded only by available cash. Automating the safe half first is not
--   timidity; it is the order in which the two failures differ.
--
-- ── RUNNERS: why the hard target stops being unconditional ─────────────────
--
-- Measured over the 70 realised trades: median capture ratio 3%. Nearly all of
-- the favourable excursion was given back. The old rule exited everything at
-- 3R, which treats every trade identically at exactly the moment they stop
-- being identical — a stock grinding sideways at 3R and a stock in a genuine
-- multi-week trend at 3R got the same treatment, and the trend is what pays for
-- the losers.
--
-- At the target the system now assesses the trend on evidence it already
-- computes — structure, momentum, participation, relative strength, sector
-- rank — and either banks it or converts to a runner with a wider trail.
--
-- This is safe to automate for one specific reason: a position at the target
-- has already booked half at 1.5R and its stop is at or above breakeven, so
-- running risks OPEN PROFIT, never capital. The loss side keeps no discretion
-- whatsoever.
--
-- Verified against the live book on 2026-07-28:
--   PPLPHARMA  STRONG 88%  (+11.8% over 50DMA, RSI 67, ADX 29, 2.0x volume,
--                           RS +15.0, sector rank 1)              -> RUN
--   BHEL       FADING 50%  (volume 0.5x drying up, sector rank 11
--                           rotating out)                         -> EXIT
-- ═══════════════════════════════════════════════════════════════════════════


-- ── Phase 3 ────────────────────────────────────────────────────────────────
UPDATE public.system_config SET value = '3.0'  WHERE key = 'intraday_autonomy_phase';
UPDATE public.system_config SET value = 'true' WHERE key = 'intraday_orders_enabled';
UPDATE public.system_config SET value = 'true' WHERE key = 'intraday_auto_exit';

INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('swing_auto_exit', 'true',
   'Automate exits and partial books on SWING positions. Separate from intraday because the risk differs: an intraday exit is bounded and same-day, a swing exit disposes of a position built over weeks.',
   'Positions', 'execution/gates.py', 'bool', 'CRITICAL'),

  -- Swing rails. Larger and rarer than intraday, so a cap tuned for intraday
  -- would block every legitimate swing exit.
  ('swing_max_order_value', '25000',
   'Hard rupee ceiling per swing order, independent of what sizing computes. Sizing bugs are what empty an account and every sizing input here has been wrong at some point.',
   'Positions', 'execution/gates.py', 'float', 'CRITICAL'),
  ('swing_max_orders_per_day', '4',
   'Daily swing order cap.', 'Positions', 'execution/gates.py', 'int', 'CRITICAL'),
  ('swing_max_notional_per_day', '50000',
   'Daily swing notional cap.', 'Positions', 'execution/gates.py', 'float', 'CRITICAL'),

  -- ── Runner logic ────────────────────────────────────────────────────────
  ('exit_runners_enabled', 'true',
   'At the hard target, assess the trend and either bank it or run it. Off reverts to an unconditional exit at target_r.',
   'Exit policy', 'control/exit_rules.py', 'bool', 'CRITICAL'),
  ('exit_runner_trail_r', '2.0',
   'Trail width for a runner, in R below the high-water mark. Wider than the normal 1.5R trail: a runner is being given room to breathe, which is the entire point.',
   'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_max_runners', '2',
   'How many positions may be runners at once. Concentration risk rises when several positions are all being given extra room simultaneously.',
   'Exit policy', 'control/exit_rules.py', 'int', 'TUNING'),

  -- Thresholds for "is this trend still worth holding". Fractions of the
  -- checks that passed, so they stay meaningful as checks are added.
  ('exit_runner_strong_score', '0.75', 'Fraction of trend checks passing to call it STRONG.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_intact_score', '0.55', 'Threshold for INTACT — still eligible to run.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_broken_score', '0.30', 'Below this the setup is BROKEN and profit is taken.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_rsi_min', '50',  'RSI floor for a trend to count as intact.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_rsi_exhausted', '82', 'RSI above which the move is treated as exhausted rather than strong.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_min_adx', '22', 'ADX floor — below this the trend is losing definition.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_min_vol_ratio', '0.75', 'Volume floor. A trend nobody is trading is not a trend.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),
  ('exit_runner_max_sector_rank', '8', 'Sector rank beyond which the sector is treated as rotating out.', 'Exit policy', 'control/exit_rules.py', 'float', 'TUNING'),

  -- ── Deterioration exit ──────────────────────────────────────────────────
  ('exit_deterioration_enabled', 'true',
   'Exit a PROFITABLE position whose thesis has broken, before the trail catches it. The trail reacts to price after the damage; this reacts to the reason while there is still profit to protect.',
   'Exit policy', 'control/exit_rules.py', 'bool', 'CRITICAL'),
  ('exit_deterioration_min_r', '1.0',
   'Minimum profit before deterioration can trigger. Below it the ordinary stop is the right tool, and second-guessing a working trade on indicator noise cuts good positions early.',
   'Exit policy', 'control/exit_rules.py', 'float', 'TUNING')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE ph text; orders text; ie text; se text;
BEGIN
  SELECT value INTO ph     FROM public.system_config WHERE key = 'intraday_autonomy_phase';
  SELECT value INTO orders FROM public.system_config WHERE key = 'intraday_orders_enabled';
  SELECT value INTO ie     FROM public.system_config WHERE key = 'intraday_auto_exit';
  SELECT value INTO se     FROM public.system_config WHERE key = 'swing_auto_exit';
  RAISE NOTICE '';
  RAISE NOTICE 'PHASE % — orders_enabled=% | intraday_auto_exit=% | swing_auto_exit=%',
               ph, orders, ie, se;
  RAISE NOTICE 'The system can now SELL without asking. Entries remain manual.';
  RAISE NOTICE 'Caps: swing 25,000/order 4/day; intraday 10,000/order 5/day.';
  RAISE NOTICE 'DRY_RUN in .env is a fourth gate and overrides all of the above.';
  RAISE NOTICE 'Kill switch: UPDATE system_config SET value=''true'' WHERE key=''master_kill_switch'';';
END $$;
