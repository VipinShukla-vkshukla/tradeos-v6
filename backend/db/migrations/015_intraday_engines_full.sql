-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 015: the remaining intraday engines + index gate
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 014 shipped two engines. Two is not a framework — it is two opinions about
-- two market behaviours, and it leaves the system idle on every session that
-- does neither. The full set covers the behaviours that actually occur:
--
--   BREAKS A LEVEL     ORB  today's opening range
--                      GAP  overnight repricing that holds
--                      PDL  yesterday's high, broken and retested
--                      VCE  a compression releasing
--   CONTINUES A TREND  PBK  the first pullback to VWAP on a trend day
--   NOTHING BREAKS     VWR  fade below VWAP and reclaim
--                      RNG  the low of a range proven on both sides
--
-- That last family matters most for coverage. Most sessions do not trend, and a
-- system built only of breakout engines produces nothing on the majority of
-- days while feeling like it is working.
--
-- THE INDEX GATE
-- --------------
-- Seven long-only engines, each evaluating one stock with no idea what the
-- market is doing, will all find "strength" on a day the index is breaking
-- down — and produce a cluster of correlated losses that looked like seven
-- independent signals. Intraday correlation rises sharply in selloffs. RISK_OFF
-- therefore BLOCKS rather than shrinks: there is no size at which buying into a
-- broad selloff becomes a good intraday trade.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_strategies_enabled', 'true', 'Master switch for the dedicated intraday engines. Off leaves position monitoring and the swing entry-approach watch running.', 'Intraday', 'intraday/engine.py', 'bool', 'CRITICAL'),
  ('intraday_bar_interval', 'minute', 'Candle interval requested from Kite historical data.', 'Intraday', 'intraday/engine.py', 'string', 'TUNING'),

  -- ── Index gate ───────────────────────────────────────────────────────────
  ('mkt_size_risk_on',  '1.0',  'Position size multiplier when the index is above VWAP and rising.', 'Intraday', 'intraday/market_context.py', 'float', 'CRITICAL'),
  ('mkt_size_neutral',  '0.6',  'Multiplier in mixed conditions. Also the default when index data is unavailable — an unknown market is not a good one.', 'Intraday', 'intraday/market_context.py', 'float', 'CRITICAL'),
  ('mkt_size_caution',  '0.35', 'Multiplier when the index is under its own VWAP.', 'Intraday', 'intraday/market_context.py', 'float', 'CRITICAL'),
  ('mkt_risk_on_min_chg_pct', '0.25', 'Index change needed for full size rather than neutral.', 'Intraday', 'intraday/market_context.py', 'float', 'TUNING'),

  -- ── GAP ──────────────────────────────────────────────────────────────────
  ('intraday_engine_gap_enabled', 'true', 'Gap-and-go engine.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'bool', 'TUNING'),
  ('gap_min_pct',          '0.60', 'Minimum gap to consider.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),
  ('gap_min_atr_frac',     '0.25', 'Gap floor as a fraction of daily ATR% — below this it is noise, not repricing.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),
  ('gap_max_atr_frac',     '1.60', 'Gap ceiling as a fraction of daily ATR% — above this it is exhaustion.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),
  ('gap_hold_window_min',  '15',   'Window whose low must hold for the gap to count as held.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'int', 'TUNING'),
  ('gap_max_retrace_pct',  '50',   'How much of the gap may be given back.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),
  ('gap_min_volume_ratio', '1.30', 'Volume confirmation — a gap on thin volume is a quote adjustment.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),
  ('gap_stop_buffer_pct',  '0.10', 'Buffer below the hold low.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),
  ('gap_max_risk_pct',     '1.30', 'Cap on stop distance.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'CRITICAL'),
  ('gap_target_r',         '2.0',  'Target multiple of risk.', 'Intraday', 'intraday/strategies/gap_and_go.py', 'float', 'TUNING'),

  -- ── PDL ──────────────────────────────────────────────────────────────────
  ('intraday_engine_pdl_enabled', 'true', 'Previous-day-high break and retest.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'bool', 'TUNING'),
  ('pdl_break_margin_pct', '0.08', 'How decisively the level must have been broken.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'TUNING'),
  ('pdl_retest_tol_pct',   '0.30', 'How close to the level counts as a retest.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'TUNING'),
  ('pdl_hold_bars',        '3',    'Recent bars that must not have closed back below.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'int', 'TUNING'),
  ('pdl_fail_margin_pct',  '0.15', 'How far below the level counts as failure.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'TUNING'),
  ('pdl_max_retest_volume_ratio', '2.5', 'A retest on heavy volume is distribution, not support.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'TUNING'),
  ('pdl_stop_buffer_pct',  '0.25', 'Buffer below the level.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'TUNING'),
  ('pdl_max_risk_pct',     '0.80', 'Cap on stop distance.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'CRITICAL'),
  ('pdl_target_r',         '2.5',  'Fallback target multiple.', 'Intraday', 'intraday/strategies/prev_day_levels.py', 'float', 'TUNING'),

  -- ── VCE ──────────────────────────────────────────────────────────────────
  ('intraday_engine_vce_enabled', 'true', 'Volatility contraction then expansion — the pattern most likely to clear costs.', 'Intraday', 'intraday/strategies/squeeze.py', 'bool', 'TUNING'),
  ('vce_window_bars',      '8',    'Bars per comparison window.', 'Intraday', 'intraday/strategies/squeeze.py', 'int', 'TUNING'),
  ('vce_max_contraction_ratio', '0.65', 'Recent range vs prior range. Lower means tighter coil.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'TUNING'),
  ('vce_min_range_atr_frac','0.08','Floor on coil size so the target can still clear costs.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'TUNING'),
  ('vce_max_chase_pct',    '0.45', 'How far past the coil high an entry is allowed.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'TUNING'),
  ('vce_min_volume_ratio', '1.40', 'Release must carry participation, or it is just widening.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'TUNING'),
  ('vce_stop_buffer_pct',  '0.10', 'Buffer below the coil low.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'TUNING'),
  ('vce_max_risk_pct',     '1.00', 'Cap on stop distance.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'CRITICAL'),
  ('vce_target_r',         '2.5',  'Fallback target multiple; the measured move usually wins.', 'Intraday', 'intraday/strategies/squeeze.py', 'float', 'TUNING'),

  -- ── PBK ──────────────────────────────────────────────────────────────────
  ('intraday_engine_pbk_enabled', 'true', 'First pullback to VWAP on a trend day.', 'Intraday', 'intraday/strategies/pullback.py', 'bool', 'TUNING'),
  ('pbk_min_bars',         '15',   'Minimum session length before a trend day can be called.', 'Intraday', 'intraday/strategies/pullback.py', 'int', 'TUNING'),
  ('pbk_min_frac_above_vwap','0.70','Fraction of bars above VWAP required to call it a trend day.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'TUNING'),
  ('pbk_max_dist_vwap_pct','0.45', 'How far above the anchor an entry is still a pullback.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'TUNING'),
  ('pbk_min_off_high_pct', '0.30', 'Minimum distance off the high — at the high there is nothing to buy.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'TUNING'),
  ('pbk_touch_tol_pct',    '0.25', 'Proximity that counts as touching the anchor.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'TUNING'),
  ('pbk_max_touches',      '2',    'Each successive test is weaker; this is what separates trading a trend from trading its exhaustion.', 'Intraday', 'intraday/strategies/pullback.py', 'int', 'TUNING'),
  ('pbk_structure_bars',   '10',   'Bars checked for higher-low structure.', 'Intraday', 'intraday/strategies/pullback.py', 'int', 'TUNING'),
  ('pbk_stop_buffer_pct',  '0.15', 'Buffer below the anchor.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'TUNING'),
  ('pbk_max_risk_pct',     '0.80', 'Cap on stop distance.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'CRITICAL'),
  ('pbk_target_r',         '2.5',  'Fallback target multiple.', 'Intraday', 'intraday/strategies/pullback.py', 'float', 'TUNING'),

  -- ── RNG ──────────────────────────────────────────────────────────────────
  ('intraday_engine_rng_enabled', 'true', 'Range-low fade — the engine that covers non-trending sessions.', 'Intraday', 'intraday/strategies/range_fade.py', 'bool', 'TUNING'),
  ('rng_min_bars',         '20',   'Minimum session length before a range can be called established.', 'Intraday', 'intraday/strategies/range_fade.py', 'int', 'TUNING'),
  ('rng_min_width_atr_frac','0.35','Range must be wide enough that edge-to-edge clears costs.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING'),
  ('rng_max_width_atr_frac','1.10','Above this it is a trend, not a range.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING'),
  ('rng_touch_tol_pct',    '0.20', 'Proximity that counts as touching an edge.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING'),
  ('rng_min_touches_each', '2',    'Confirmed touches required on BOTH edges — two points define a line and prove nothing.', 'Intraday', 'intraday/strategies/range_fade.py', 'int', 'TUNING'),
  ('rng_max_dist_from_low_pct','0.25','How close to the low an entry must be.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING'),
  ('rng_max_volume_ratio', '1.80', 'A low tested on heavy volume is usually breaking, not holding.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING'),
  ('rng_stop_buffer_pct',  '0.25', 'Buffer below the range low.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING'),
  ('rng_max_risk_pct',     '0.70', 'Cap on stop distance.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'CRITICAL'),
  ('rng_target_frac',      '0.85', 'Exit inside the opposite edge — where the sellers are known to be.', 'Intraday', 'intraday/strategies/range_fade.py', 'float', 'TUNING')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.system_config WHERE category = 'Intraday';
  RAISE NOTICE 'Intraday config keys: %', n;
  RAISE NOTICE 'Seven engines: ORB GAP PDL VCE (levels) · PBK (trend) · VWR RNG (no-break coverage)';
  RAISE NOTICE 'Index gate active — RISK_OFF blocks new longs entirely rather than shrinking them.';
END $$;
