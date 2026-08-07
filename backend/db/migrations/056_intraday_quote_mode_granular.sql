-- Splits intraday_quote_mode into two independently-gated switches, both
-- defaulting to the SAME 'true' migration 043 already set for the single
-- switch this replaces. This is a regrouping for independent control and
-- ongoing verification, not a behaviour change -- applying this migration
-- changes nothing about what has been live since Phase 4 (05-Aug-2026).
--
-- tools/quote_parity.py measured the six overlaid fields on 07-Aug-2026 and
-- day_high/day_low/day_open held cleanly (0 behind — RATCHET_UP/RATCHET_DOWN
-- in quote_parity.py) while vwap and prev_close both showed FAULT against
-- the blanket 0.10% band (vwap worst -0.39%, prev_close worst -4.18%).
-- Neither is being turned off on that measurement, for reasons specific to
-- each -- see intraday/engine.py::apply_live_quotes()'s docstring for the
-- full reasoning:
--   - prev_close's live side is Kite's own broker-reported previous close;
--     its fetched side is a stock_data_daily lookup with an unconfirmed but
--     plausible bug (a global, not per-symbol, row LIMIT that can resolve
--     to a stale multi-day-old close). The more likely direction is that
--     the live overlay is CORRECTING that bug, not disagreeing with a
--     correct fetched value -- pulling it out on an unconfirmed guess about
--     which side is wrong would be a regression, not a fix.
--   - vwap's FAULT is a formula difference (live tick VWAP vs
--     refresh_contexts()'s bar-approximation), not staleness, so some
--     disagreement is structural and expected. It has been live since
--     Phase 4 without an incident being traced to it; turning it off now
--     would be exactly the kind of one-day-snapshot-becomes-permanent-
--     decision this migration is trying to avoid making blind.
--
-- What actually changes: this is now split into two switches instead of
-- one, so either can be turned off independently later without a code
-- change if evidence ever supports it -- and tools/health.py's
-- check_quote_parity, added alongside this, watches both continuously
-- (fails if a switch is on but parity logging isn't, or if recent data
-- disagrees), so a regression on some future session gets caught by the
-- check run before every trading day rather than assumed safe forever on
-- this one measurement.
--
-- This migration:
--   1. DELETES the old switch. Nothing reads it after this branch's code
--      change (intraday/engine.py, intraday/price_feed.py, health.py,
--      quote_parity.py, the operator panel) -- confirmed by grep across the
--      whole repo before writing this. A row nothing reads is exactly the
--      kind of artifact that misleads the next person who inspects
--      system_config directly and assumes it still controls something.
--   2. Adds two new switches, BOTH DEFAULT TRUE — day_open/day_high/
--      day_low/volume/prev_close, and vwap separately. Applying this
--      migration alone changes nothing live; it only adds the independent
--      toggles and the vocabulary tools.health now watches.

DELETE FROM public.system_config
 WHERE key = 'intraday_quote_mode';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_quote_mode_range', 'true',
   'When true, intraday/engine.py::apply_live_quotes() overlays the live '
   'websocket day_open/day_high/day_low/volume/prev_close onto contexts '
   'every 15s cycle, instead of leaving them at the 300s-refresh_contexts() '
   'value. DEFAULT TRUE — matches what migration 043 already set for the '
   'single switch this replaces; not a new behaviour. MEASURED 07-Aug-2026 '
   '(tools/quote_parity.py): day_high/day_low held cleanly (0 behind); '
   'prev_close FAULTed against the fetched side but the live value here is '
   'the broker''s own previous close, more likely correcting a '
   'stock_data_daily bug than causing one — see '
   'intraday/engine.py::apply_live_quotes() docstring. Watched continuously '
   'by tools.health''s check_quote_parity. Independent of '
   'intraday_quote_mode_vwap -- enabling/disabling one never affects the other.',
   'Master controls', 'intraday/engine.py', 'bool', 'true', 'MEDIUM'),
  ('intraday_quote_mode_vwap', 'true',
   'When true, intraday/engine.py::apply_live_quotes() overlays the live '
   'websocket VWAP (Kite''s average_traded_price, true tick-weighted) onto '
   'contexts, replacing refresh_contexts()''s bar-approximation VWAP '
   '((H+L+C)/3 weighted by minute-bar volume). DEFAULT TRUE — matches what '
   'migration 043 already set; not a new behaviour. MEASURED 07-Aug-2026: '
   'FAULT, 7 of 2880 beyond 0.10%, worst -0.39% -- a formula disagreement, '
   'not staleness, so some gap is structural and expected. Watched '
   'continuously by tools.health''s check_quote_parity '
   '(tools/quote_parity.py::vwap_verdict, scored against the tightest '
   'tolerance any engine that reads vwap actually uses, not the blanket '
   '0.10% band) -- if this starts moving a real engine decision, that check '
   'will fail before every trading session rather than this being '
   'rediscovered by hand. Independent of intraday_quote_mode_range.',
   'Master controls', 'intraday/engine.py', 'bool', 'true', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
