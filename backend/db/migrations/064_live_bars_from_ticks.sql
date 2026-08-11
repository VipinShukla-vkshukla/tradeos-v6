-- 11-Aug-2026. Bars for SymbolContext were built exclusively from
-- kite.historical_data() -- one rate-limited REST call per symbol, refreshed
-- once per 300s slow tick, and therefore capped at intraday_max_universe
-- (40) names. A name outside that 40 was invisible to every engine
-- regardless of how it was moving right now, and even a watched name's
-- bars were up to five minutes stale between refreshes.
--
-- intraday/bar_builder.py now aggregates the tick stream the websocket
-- already carries into the same Bar shape, at zero incremental API cost,
-- and engine.py::merge_live_bars() extends context bars from it every 15s
-- for the FULL bench (intraday_universe_bench_size, 120), not just the 40.
-- See that function's docstring for the full mechanism.
--
-- Both switches are rollback levers, not required tuning:
--   intraday_live_bars_enabled=false restores exactly today's behaviour
--   (bars only from historical_data, only for the top 40).
--   intraday_min_live_bars_for_context raises or lowers how many live
--   minute bars a bench-only name needs before an engine is allowed to
--   read a context built from them at all -- mirrors the existing
--   `len(raw) < 5` guard on the historical side.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_live_bars_enabled', 'true',
   'Master switch for merge_live_bars() -- extending/building SymbolContext '
   'bars from the tick stream (bar_builder.py) instead of relying solely on '
   'the 300s historical_data() refresh. OFF restores exactly the pre-11-Aug '
   'behaviour: bars only for the top intraday_max_universe names, only from '
   'historical_data, only every 300s. The immediate rollback lever if the '
   'live-built bars ever look wrong.',
   'Master controls', 'intraday/bar_builder.py', 'bool', 'true', 'MEDIUM'),
  ('intraday_min_live_bars_for_context', '5',
   'Minimum completed live minute bars a bench-only symbol must have before '
   'merge_live_bars() will stand up a SymbolContext for it at all. Mirrors '
   'refresh_contexts()''s own historical-side guard (len(raw) < 5) -- a '
   'name that started ticking thirty seconds ago must read as "not enough '
   'bars yet", not be handed to an engine as a thin, misleading context.',
   'Master controls', 'intraday/bar_builder.py', 'int', '5', 'LOW')
ON CONFLICT (key) DO NOTHING;
