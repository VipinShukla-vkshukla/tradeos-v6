-- 10-Aug-2026. intraday/scanner.py's universe selection was entirely
-- prior-day (stock_data_daily), computed once and cached until the calendar
-- date changed -- correct for the tradability/liquidity/movement/quality
-- FILTER (slow-moving facts) but wrong for the RANKING: a name that went
-- quiet today kept its websocket slot for the whole session, and a name
-- outside yesterday's static top 40 that started moving hard mid-morning
-- was never seen at all, because nothing subscribed to it.
--
-- Operator's explicit preference, verbatim: "Decide if it needs to be 15
-- second loop or 300s timer my preference is 15 second but I will elt you
-- take that call." Shipped as a 15s re-rank (scanner.live_rerank(), called
-- every cycle from IntradayEngine.rerank_universe_live()) over a wider
-- LTP-only bench (watch_symbols() now subscribes to it), while the
-- EXPENSIVE resource the 40-cap exists to protect -- historical_data bars,
-- via context_symbols()/refresh_contexts() -- stays on the existing 300s
-- timer untouched. The re-rank itself is pure arithmetic over ticks the
-- socket already carries, so running it every 15s costs nothing extra; only
-- the bar-fetch consequence of a promotion is bounded to the slower cadence.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_universe_bench_size', '120',
   'Size of the wider, LTP/quote-only candidate pool (scanner.universe()) '
   'that the live re-rank promotes from, on top of the intraday_max_universe '
   '(40) names that actually get bars. Websocket-only cost -- Kite prices '
   'this at up to 3000 instruments -- not the rate-limited historical_data '
   'budget the 40-cap protects.',
   'Master controls', 'intraday/scanner.py', 'int', '120', 'LOW'),

  ('intraday_universe_live_rerank', 'true',
   'Master switch for the 15s live re-rank (scanner.live_rerank(), called '
   'from IntradayEngine.rerank_universe_live()). OFF falls back exactly to '
   'the prior static, once-a-day ranking -- the immediate rollback lever if '
   'the live signal ever looks wrong.',
   'Master controls', 'intraday/scanner.py', 'bool', 'true', 'MEDIUM'),

  ('intraday_rerank_live_weight', '0.4',
   'Blend weight of the live RVOL score against each entry''s static score '
   'in live_rerank() -- 0 = pure static (today''s old behaviour), 1 = pure '
   'live volume. A name with no live tick yet keeps its static score '
   'unchanged regardless of this weight; it only applies once a live volume '
   'reading exists.',
   'Scoring', 'intraday/scanner.py', 'float', '0.4', 'MEDIUM'),

  ('intraday_rvol_cap', '3.0',
   'Relative-volume multiple (today''s traded volume so far, vs the '
   '20-day average scaled to the same elapsed session time) at which the '
   'live component of live_rerank() saturates to its maximum score. 3x is '
   'the conventional "unusually active" threshold; raising it makes the '
   'live signal reward only more extreme volume days.',
   'Scoring', 'intraday/scanner.py', 'float', '3.0', 'LOW')
ON CONFLICT (key) DO NOTHING;
