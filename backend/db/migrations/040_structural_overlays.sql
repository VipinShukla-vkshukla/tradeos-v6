-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 040: structural overlays (Stage 9)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Three overlays that generate approximately zero gross alpha and a meaningful
-- amount of net alpha, entirely by removing realisations from the left tail.
-- None of them finds a trade. Nothing here will ever show up as a winning
-- position — their contribution is the losses that did not happen, which no P&L
-- line reports. Worth stating plainly, because it makes them easy to switch off
-- during a good run, which is exactly when they are earning their place.
--
-- ALL THREE DEFAULT OFF IN CODE AND ON HERE, so a database without this
-- migration behaves exactly as it does today.
--
-- 1. EXPIRY DAY-TYPE
--    On index expiry the cash tape is a different animal — pinning, unwinding
--    flows, late-session gamma — and it alters precisely the two archetypes the
--    intraday sleeve trades: opening-range breakouts and VWAP reversion. An
--    engine calibrated across all days is calibrated for the ~80% of sessions
--    when settlement flows do not dominate, and quietly wrong on the rest.
--
--    It SIZES DOWN rather than excluding. Expiry days are not untradeable, they
--    are differently distributed with fatter tails in both directions;
--    excluding them forfeits the sessions with the largest intraday ranges.
--
--    THE DAY-TYPE FLAG IS A HEURISTIC AND IT IS APPROXIMATELY RIGHT.
--    No F&O expiry calendar is ingested, so the last trading day of the week is
--    treated as weekly expiry and the last of the month as monthly. Real NSE
--    monthly expiry is the last THURSDAY, so expect a one-to-three session
--    error on the monthly flag most months. Tolerable here because the overlay
--    only reduces size — a false positive costs a little participation, a false
--    negative leaves the day exactly as it is today. It would NOT be tolerable
--    as an input to a prior fitted per day-type, which needs a real calendar
--    first. Say so before anyone builds that.
--
-- 2. VOLATILITY-REGIME EXPOSURE SCALING
--    A momentum book bleeds in high-volatility regimes: stops are hit by noise
--    rather than by thesis failure, and the same 1% risk buys a much wider
--    distribution of outcomes. Shrinking exposure into those regimes is the
--    cheapest drawdown control available, and costs return only in the minority
--    of high-VIX periods that also trend.
--
--    Bands come from THIS ACCOUNT'S observed VIX distribution, not from
--    textbook levels. 94 readings to 04-Aug-2026: p25 13.4, median 16.6,
--    p75 19.6, max 27.9. A textbook "above 20 is high" would have called the
--    entire last quarter calm and never fired once.
--
-- 3. LIQUIDITY AND CIRCUIT-BAND ELIGIBILITY
--    A stop is a plan for continuous prices, and Indian small caps do not always
--    offer continuous prices. A breakout into a 5% band cannot travel to its
--    target; a held position gapping through its stop realises a multiple of
--    planned R, because the broker-side stop becomes a market order and fills
--    wherever the gap opens.
--
--    Measured against the name's OWN traded value rather than an absolute
--    floor: Rs 4,000 is nothing in RELIANCE and a meaningful share of a day's
--    turnover in a micro-cap.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('overlay_expiry_enabled', 'true',
   'Condition intraday sizing on derivative settlement day-type. Off treats '
   'every session as NORMAL, which is prior behaviour.',
   'Intraday', 'analysis/overlays.py', 'bool', 'false', 'CRITICAL'),
  ('overlay_size_expiry_week', '0.85',
   'Size multiplier during an expiry week.',
   'Intraday', 'analysis/overlays.py', 'float', '0.85', 'TUNING'),
  ('overlay_size_expiry_day', '0.70',
   'Size multiplier on weekly expiry.',
   'Intraday', 'analysis/overlays.py', 'float', '0.70', 'TUNING'),
  ('overlay_size_monthly_expiry', '0.55',
   'Size multiplier on monthly expiry, when settlement flows dominate most.',
   'Intraday', 'analysis/overlays.py', 'float', '0.55', 'TUNING'),

  ('overlay_vol_scaling_enabled', 'true',
   'Scale BOOK-LEVEL exposure inversely to India VIX. Off means flat exposure '
   'in every volatility regime, which is prior behaviour.',
   'RISK', 'analysis/overlays.py', 'bool', 'false', 'CRITICAL'),
  ('overlay_vix_calm', '14.0',
   'VIX at or below this is calm: full exposure. Set from the p25 of 94 '
   'observed readings (13.4), not from a textbook level.',
   'RISK', 'analysis/overlays.py', 'float', '14.0', 'TUNING'),
  ('overlay_vix_high', '20.0',
   'Above this, exposure drops to overlay_exposure_high. Near the observed p75 '
   'of 19.6.',
   'RISK', 'analysis/overlays.py', 'float', '20.0', 'TUNING'),
  ('overlay_vix_fear', '26.0',
   'Above this, minimum exposure. Near the observed maximum of 27.9.',
   'RISK', 'analysis/overlays.py', 'float', '26.0', 'TUNING'),
  ('overlay_exposure_normal', '0.85',
   'Book exposure between calm and high VIX.',
   'RISK', 'analysis/overlays.py', 'float', '0.85', 'TUNING'),
  ('overlay_exposure_high', '0.60',
   'Book exposure in an elevated-volatility regime.',
   'RISK', 'analysis/overlays.py', 'float', '0.60', 'TUNING'),
  ('overlay_exposure_fear', '0.35',
   'Book exposure in an extreme-volatility regime. A momentum book here is '
   'paying noise to discover that its stops were too tight.',
   'RISK', 'analysis/overlays.py', 'float', '0.35', 'TUNING'),

  ('overlay_liquidity_enabled', 'true',
   'Refuse names that cannot be exited at plan. Generates zero gross alpha and '
   'removes left-tail realisations. Off is prior behaviour.',
   'RISK', 'analysis/overlays.py', 'bool', 'false', 'CRITICAL'),
  ('overlay_min_daily_value_cr', '5.0',
   'Minimum daily traded value in crore. Below this a stop is a suggestion.',
   'RISK', 'analysis/overlays.py', 'float', '5.0', 'TUNING'),
  ('overlay_max_share_of_daily_value', '0.005',
   'Largest share of a name daily traded value that one position may represent. '
   'Above it, exiting at plan is not realistic.',
   'RISK', 'analysis/overlays.py', 'float', '0.005', 'TUNING'),
  ('overlay_band_atr_pct', '9.0',
   'ATR percent above which a name is treated as band-limited or gap-prone. A '
   'proxy: NSE circuit bands are in no feed this system ingests, and realised '
   'range is the cheapest available signal.',
   'RISK', 'analysis/overlays.py', 'float', '9.0', 'TUNING'),
  ('overlay_liquidity_strict', 'true',
   'Refuse a name whose traded value is unknown. Names with unrecorded turnover '
   'are disproportionately the illiquid ones, so unknown is treated as a no.',
   'RISK', 'analysis/overlays.py', 'bool', 'true', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Structural overlays live. All three only ever REDUCE:';
  RAISE NOTICE '  expiry     sizes intraday down on settlement-dominated days';
  RAISE NOTICE '  volatility scales book exposure down as VIX rises';
  RAISE NOTICE '  liquidity  refuses names that cannot be exited at plan';
  RAISE NOTICE '';
  RAISE NOTICE 'None will ever appear as a winning trade. Their contribution is';
  RAISE NOTICE 'the losses that did not happen, which no P&L line reports — so';
  RAISE NOTICE 'they are easiest to switch off during exactly the run in which';
  RAISE NOTICE 'they are earning their place.';
END $$;
