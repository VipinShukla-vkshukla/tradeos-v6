-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 046: the short engine, its gates, and its solvency filter
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 045 built the sign convention. This adds the parts that decide WHETHER to
-- short and WHICH names can be shorted at all, and registers the engine.
--
-- SDN STARTS SHADOW. It evaluates every cycle and records every detection, and
-- receives no capital. That is the same contract PEAD and ACC ran under, and it
-- is the only way `outcomes.resolve_day` can accumulate the resolved detections
-- that a promotion argument has to be made from. A short family promoted on
-- reasoning rather than on its own scored outcomes would be indistinguishable
-- from one promoted on hope.
--
-- THE ONE THING THAT IS NOT A QUALITY FILTER
--
-- `intraday/shortability.py` asks whether a position can be CLOSED, not whether
-- the trade is good. It runs before conviction, before cost, before the
-- allocator, and no amount of edge outweighs a refusal from it:
--
--   · A stock locked at its UPPER circuit has buyers queued and NO sellers.
--     There is no price at which a short can be covered. The stop is a number
--     in a database and the exchange does not care about it.
--   · The position is then carried to the close, which is SHORT DELIVERY: the
--     exchange buys it back in the auction session and charges the difference
--     plus a penalty commonly around 20% of the trade value.
--
-- NSE CIRCUIT BANDS ARE IN NO FEED THIS SYSTEM INGESTS. That is not an
-- assumption — migration 040 records it explicitly while introducing
-- `overlay_band_atr_pct` as a deliberate proxy. So the upper-band guard here is
-- a PROXY built from data that does exist (today's move against prev_close, and
-- the name's own daily ATR) and every message it emits says PROXY. It does not
-- need to be precise to be useful; it needs to be conservative, and it is.
--
-- WHY DELIVERY PERCENTAGE INVERTS
--
-- `scanner.py` reads high delivery as QUALITY — stock being accumulated rather
-- than churned, which is what you want to be long. For a short it is the same
-- number read the other way: stock in delivery is stock no longer available to
-- trade, and a tightly held float is the definition of a squeeze candidate.
--
-- WHY THE LIQUIDITY BAR IS DOUBLED RATHER THAN REUSED
--
-- Covering is compulsory and time-boxed; selling a long is neither. The same
-- name can be an acceptable long and an unacceptable short.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  -- ── SHADOW. Evidence before capital. ──────────────────────────────────────
  ('intraday_engine_sdn_lifecycle', 'SHADOW',
   'SDN (the short family) evaluates and records but receives no capital. '
   'Promote to ACTIVE only on its OWN resolved detections in intraday_setups, '
   'scored by outcomes.resolve_day — never on reasoning about the setups.',
   'Intraday', 'intraday/strategies/registry.py', 'text', 'SHADOW', 'CRITICAL'),

  -- ── Market context: when shorts are with the tape ────────────────────────
  ('mkt_short_size_risk_off', '0.6',
   'Short size multiplier in RISK_OFF — the regime a short book exists for, and '
   'the one where longs are switched off entirely. Not full size: selloffs '
   'produce the sharpest counter-rallies of the session, and a squeeze runs '
   'faster than a long unwind because the buying is forced.',
   'Intraday', 'intraday/market_context.py', 'float', '0.6', 'TUNING'),
  ('mkt_short_size_caution', '0.3',
   'Short size in CAUTION — below VWAP but holding above yesterday. A market '
   'losing its grip rather than one that has lost it, and the state that most '
   'often resolves into an afternoon recovery.',
   'Intraday', 'intraday/market_context.py', 'float', '0.3', 'TUNING'),

  -- ── Shortability: the solvency filter ────────────────────────────────────
  ('intraday_short_min_runway_min', '75',
   'Minutes to the COVER deadline a short needs before it may be opened. Longer '
   'than a long requires, because failing to close is a penalty rather than a '
   'bad trade, and the cover deadline is already earlier than the long exit.',
   'Intraday', 'intraday/shortability.py', 'int', '75', 'CRITICAL'),
  ('intraday_short_max_up_pct', '4.0',
   'PROXY upper-circuit ceiling, absolute. A name up more than this today is '
   'refused: real bands are in no feed here (see migration 040), and a stock '
   'locked at its upper circuit cannot be covered at any price.',
   'Intraday', 'intraday/shortability.py', 'float', '4.0', 'CRITICAL'),
  ('intraday_short_max_up_atr_mult', '1.5',
   'PROXY upper-circuit ceiling in the name''s own daily ATR. The tighter of '
   'this and the absolute cap wins, so a quiet name approaching a 5% band and a '
   'volatile one making an extreme move for itself are both caught.',
   'Intraday', 'intraday/shortability.py', 'float', '1.5', 'CRITICAL'),
  ('intraday_short_max_down_pct', '6.0',
   'How far a name may already have fallen before a short is refused. The move '
   'the setup wants has largely happened; what is left is a bounce that covers '
   'into a thin book. The fourth down-leg is where a short book gives back its '
   'month.',
   'Intraday', 'intraday/shortability.py', 'float', '6.0', 'TUNING'),
  ('intraday_short_max_delivery_pct', '75.0',
   'Squeeze ceiling. Stock in delivery is stock no longer available to trade, '
   'so a tightly held float is a squeeze candidate. The scanner reads this same '
   'column as QUALITY for a long — same number, opposite reading.',
   'Intraday', 'intraday/shortability.py', 'float', '75.0', 'TUNING'),
  ('intraday_short_liquidity_mult', '2.0',
   'How much more liquid a short must be than a long, as a multiple of '
   'intraday_min_value_cr. Covering is compulsory and time-boxed; selling a '
   'long is neither.',
   'Intraday', 'intraday/shortability.py', 'float', '2.0', 'CRITICAL'),

  -- ── The SDN engine's own parameters ──────────────────────────────────────
  ('intraday_short_max_chase_pct', '0.60',
   'How far past the failed level a short may still be entered. THE RULE THAT '
   'MAKES THE OTHERS WORK: the stop belongs just above that level, so an entry '
   '2% below it carries a 2% stop that no one-session target can pay for. It is '
   'also the correct trade — a trap is a trade against the buyers stuck AT the '
   'level, and once they are flushed what remains is bounce risk.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '0.60', 'TUNING'),
  ('intraday_short_min_rr', '1.3',
   'Minimum reward:risk for a short setup, before the cost model is consulted.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '1.3', 'TUNING'),
  ('intraday_short_target_atr_mult', '0.55',
   'Target as a fraction of the name''s daily ATR when the session low is not '
   'available as a level. Deliberately modest: downside is faster than upside '
   'intraday but also snaps back harder.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '0.55', 'TUNING'),
  ('intraday_short_low_room_pct', '0.20',
   'How much room above the session low counts as "the low is still ahead". '
   'Below this, price is AT the low and the target becomes an ATR-measured move '
   'instead — using the low itself there sets a target at or above the current '
   'price and refuses every genuine breakdown continuation.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '0.20', 'TUNING'),
  ('intraday_short_stop_buffer_pct', '0.12',
   'Buffer above the level a short''s stop sits at, so an ordinary wick through '
   'it does not stop the trade out.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '0.12', 'TUNING'),
  ('intraday_short_vwap_near_pct', '0.35',
   'How close to VWAP a bar must trade to count as having tested it, for the '
   'VWAP-rejection condition.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '0.35', 'TUNING'),
  ('intraday_short_trap_reentry_pct', '0.20',
   'How far back below the broken level price must fall before the failed '
   'breakout counts as a trap rather than a pause.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '0.20', 'TUNING'),
  ('intraday_short_orb_min_vol_ratio', '1.1',
   'Volume confirmation required on an opening-range breakdown. REQUIRED, not '
   'rewarded: a break on no volume is a drift, and drifts reverse.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'float', '1.1', 'TUNING'),
  ('intraday_short_min_bars', '15',
   'Minimum bars before the short family will read a session at all.',
   'Intraday', 'intraday/strategies/short_distribution.py', 'int', '15', 'TUNING')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE sw text; lc text;
BEGIN
  SELECT value INTO sw FROM public.system_config WHERE key = 'intraday_allow_shorts';
  SELECT value INTO lc FROM public.system_config WHERE key = 'intraday_engine_sdn_lifecycle';

  RAISE NOTICE '';
  RAISE NOTICE 'Migration 046 applied.';
  RAISE NOTICE '  intraday_allow_shorts        = %   (045; still the master switch)', sw;
  RAISE NOTICE '  intraday_engine_sdn_lifecycle = %', lc;
  RAISE NOTICE '';
  RAISE NOTICE 'SDN detects and records. It receives no capital in SHADOW, and the';
  RAISE NOTICE 'master switch is off on top of that — two independent doors.';
  RAISE NOTICE '';
  RAISE NOTICE 'TO SEE WHAT IT WOULD HAVE DONE, without risking anything:';
  RAISE NOTICE '    python -m tools.health         -> the `shorts` row must read 16/16';
  RAISE NOTICE '    UPDATE system_config SET value=''true'' WHERE key=''intraday_allow_shorts'';';
  RAISE NOTICE '  then after a few sessions:';
  RAISE NOTICE '    SELECT strategy, meta->>''sub_engine'' AS cond, outcome, count(*)';
  RAISE NOTICE '      FROM intraday_setups WHERE direction = ''SHORT''';
  RAISE NOTICE '     GROUP BY 1,2,3 ORDER BY 1,2;';
  RAISE NOTICE '';
  RAISE NOTICE 'Promote SDN to ACTIVE only on those resolved outcomes.';
  RAISE NOTICE '';
END $$;
