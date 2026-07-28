-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 022: swing structure, per framework
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE GAP
-- -------
-- Every engine in this system reasons about STATIC levels — the opening range
-- high, the previous day's high, VWAP, the top of a coil. None of them knew the
-- SEQUENCE those levels sit in, and the sequence is what price action is about.
--
-- A break of the opening range high means one thing when the stock has made
-- higher highs all morning and something entirely different when it has made
-- lower highs and merely reclaimed one level. Both looked identical to ORB. The
-- first is continuation; the second is a reversal attempt — the highest-quality
-- entry in price action once confirmed, and the most expensive one before.
--
-- THE SEQUENCE THAT MATTERS, IN ORDER
--   1. downtrend      lower highs and lower lows
--   2. higher HIGH    the first break of the sequence. NOT a reversal yet.
--                     Most fail. A warning, not an entry.
--   3. higher LOW     the confirmation — the pullback held above the prior low,
--                     so the sellers who made it are gone rather than absent
--   4. break of HH    the entry the engines already recognise
--
-- REVERSAL_UP (step 2) is BLOCKED by default. Acting on it is how a downtrend
-- takes your money twice. Waiting for step 3 costs some upside and removes most
-- of the failures.
--
-- WHY THE SETTINGS DIFFER BY FRAMEWORK
-- ------------------------------------
-- The concept is timeframe-agnostic; the parameters cannot be. A daily bar is a
-- day of price discovery, a one-minute bar is frequently noise — so one pivot
-- width applied to both would either miss every swing turn or invent an
-- intraday one every few minutes.
--
--   pivot_k       3 intraday vs 2 swing. Minute bars alternate constantly, and
--                 a 2-bar fractal on them marks noise as structure.
--   tolerance     0.15% intraday vs 0.50% swing. Two daily pivots 0.1% apart
--                 are not a meaningful statement about who is in control.
--   lookback      120 intraday bars (this session) vs 60 daily (weeks).
--                 Yesterday's intraday structure has no bearing on today's.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('structure_pivot_k', '2',
   'Default bars each side needed to confirm a pivot. Framework-specific keys override it.',
   'Signal generation', 'analysis/market_structure.py', 'int', 'TUNING'),
  ('structure_tolerance_pct', '0.10',
   'Default: how much higher a high must be to count as HIGHER.',
   'Signal generation', 'analysis/market_structure.py', 'float', 'TUNING'),

  -- Swing: daily bars, weeks of context
  ('structure_swing_pivot_k', '2', 'Pivot width on DAILY bars.', 'Signal generation', 'analysis/market_structure.py', 'int', 'TUNING'),
  ('structure_swing_tolerance_pct', '0.50', 'A higher high must clear the prior pivot by this much on a daily chart.', 'Signal generation', 'analysis/market_structure.py', 'float', 'TUNING'),
  ('structure_swing_lookback', '60', 'Daily bars read — roughly three months of structure.', 'Signal generation', 'analysis/market_structure.py', 'int', 'TUNING'),
  ('structure_swing_allow_reversal', '0',
   'Allow entries on an UNCONFIRMED reversal (higher high, no higher low yet). 0 = wait for the higher low.',
   'Signal generation', 'analysis/market_structure.py', 'float', 'CRITICAL'),

  -- Intraday: minute bars, this session only
  ('structure_intraday_pivot_k', '3', 'Pivot width on MINUTE bars — wider, because minute bars alternate constantly and a 2-bar fractal marks noise as structure.', 'Intraday', 'analysis/market_structure.py', 'int', 'TUNING'),
  ('structure_intraday_tolerance_pct', '0.15', 'Tighter than swing: intraday ranges are smaller, so the same percentage would be a much larger move.', 'Intraday', 'analysis/market_structure.py', 'float', 'TUNING'),
  ('structure_intraday_lookback', '120', 'Minute bars read — this session, not yesterday''s.', 'Intraday', 'analysis/market_structure.py', 'int', 'TUNING'),
  ('structure_intraday_allow_reversal', '0',
   'Allow intraday entries on an unconfirmed reversal. 0 = wait for the higher low.',
   'Intraday', 'analysis/market_structure.py', 'float', 'CRITICAL'),

  ('intraday_structure_gate', 'true',
   'Block intraday setups whose swing structure is a downtrend. Breaking the opening range high while making lower highs all morning is buying a lower high — the trade a downtrend exists to punish.',
   'Intraday', 'intraday/engine.py', 'bool', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Structure is now a first-class check in BOTH frameworks:';
  RAISE NOTICE '  intraday — blocks setups in a downtrend (3-bar pivots, this session)';
  RAISE NOTICE '  swing    — votes in the runner/trend assessment (2-bar pivots, 60 days)';
  RAISE NOTICE 'Unconfirmed reversals are blocked by default in both.';
END $$;
