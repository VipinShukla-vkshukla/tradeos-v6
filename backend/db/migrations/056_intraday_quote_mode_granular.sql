-- Splits intraday_quote_mode into per-field-group switches.
--
-- tools/quote_parity.py measured the six overlaid fields on 07-Aug-2026 and
-- they did not agree as a block: day_high/day_low/day_open held cleanly (0
-- behind — see RATCHET_UP/RATCHET_DOWN in quote_parity.py), but vwap and
-- prev_close both FAULTed (vwap worst -0.39%, prev_close worst -4.18%). The
-- single intraday_quote_mode switch meant promoting the two clean fields
-- required promoting the two faulty ones alongside them -- and, per migration
-- 043 ("v7 Phase 4 live"), intraday_quote_mode was already flipped true,
-- meaning the live paper book has been overlaying the faulty prev_close and
-- vwap values onto every breakout condition since that migration ran.
--
-- This migration:
--   1. DELETES the old switch. Nothing reads it after this branch's code
--      change (intraday/engine.py, intraday/price_feed.py, health.py,
--      quote_parity.py, the operator panel) -- confirmed by grep across the
--      whole repo before writing this. A row nothing reads is exactly the
--      kind of artifact that misleads the next person who inspects
--      system_config directly and assumes it still controls something.
--      Not a behaviour change: it was already superseded in-process by (2).
--   2. Adds two new switches, BOTH DEFAULT FALSE — day range/volume and vwap,
--      decided independently. This is a conservative landing: applying this
--      migration alone restores intraday to the same "no live overlay, REST-
--      only, 300s-refresh" state as if intraday_quote_mode had always been
--      off, until the operator explicitly re-enables one or both.
--   3. prev_close has NO switch. It is no longer overlaid under any
--      configuration -- see intraday/engine.py::apply_live_quotes()'s
--      docstring for why a live overlay was never the right fix for it (it
--      is time-invariant intraday; the FAULT traces to a correctness bug in
--      refresh_contexts()'s stock_data_daily lookup, not staleness).
--
-- Recommended sequence after applying:
--   UPDATE system_config SET value='true' WHERE key='intraday_quote_mode_range';
--   -- (day_high/day_low/day_open/volume only -- these passed parity clean)
-- Leave intraday_quote_mode_vwap off. 08-Aug-2026: checked the measured vwap
-- diffs against what the engines that actually read vwap require, not just
-- the blanket 0.10% parity band -- vwap_reclaim.py's stop buffer
-- (vwr_stop_buffer_pct, 0.08%) and short_distribution.py's stop buffer
-- (intraday_short_stop_buffer_pct, 0.12%) are BOTH tighter than the measured
-- worst-case gap (-0.39%), and vwap_reclaim.py also gates on a hard
-- ltp > vwap / close < vwap crossing with no tolerance band at all. A
-- formula swap this size can flip those decisions, not just relabel them.
-- tools/quote_parity.py's report() now prints exactly how many collected
-- comparisons would have crossed each engine's own tolerance --
-- re-run it after a logging session and use THAT number, not this comment,
-- to decide.

DELETE FROM public.system_config
 WHERE key = 'intraday_quote_mode';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_quote_mode_range', 'false',
   'When true, intraday/engine.py::apply_live_quotes() overlays the live '
   'websocket day_open/day_high/day_low/volume onto contexts every 15s '
   'cycle, instead of leaving them at the 300s-refresh_contexts() value. '
   'MEASURED 07-Aug-2026 (tools/quote_parity.py): day_high/day_low parity '
   'held cleanly across 2880 comparisons each (0 behind on either ratchet). '
   'Safe to enable on that evidence. Independent of '
   'intraday_quote_mode_vwap -- enabling one never affects the other.',
   'Master controls', 'intraday/engine.py', 'bool', 'false', 'MEDIUM'),
  ('intraday_quote_mode_vwap', 'false',
   'When true, intraday/engine.py::apply_live_quotes() overlays the live '
   'websocket VWAP (Kite''s average_traded_price, true tick-weighted) onto '
   'contexts, replacing refresh_contexts()''s bar-approximation VWAP '
   '((H+L+C)/3 weighted by minute-bar volume). MEASURED 07-Aug-2026: FAULT, '
   '7 of 2880 beyond 0.10%, worst -0.39%. This is not a staleness gap -- the '
   'two sides are different FORMULAS, so live and fetched will always '
   'diverge somewhat. Keep off until a reconciled definition is validated, '
   'not merely resynced. Independent of intraday_quote_mode_range.',
   'Master controls', 'intraday/engine.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
