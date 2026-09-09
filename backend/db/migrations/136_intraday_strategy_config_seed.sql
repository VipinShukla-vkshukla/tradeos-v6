-- 09-Sep-2026. intraday_strategy_config (migration 014) — what the frontend's
-- engine cards read their "conditions" text from (frontend/lib/supabase.ts::
-- getIntradayStrategyConfig(), "transcribed from the actual declarative
-- gates ... not copy invented for a screen") — has had ZERO rows for every
-- intraday engine, not only IGN, since the table was created. registry.py's
-- own sync_to_db() was built to populate it and was never called from
-- anywhere in this codebase — confirmed by grep, the exact "a step that
-- completes producing nothing" failure this project's own rule names. Now
-- wired into intraday/run.py's startup (alongside _rehydrate_recorded()),
-- so a future new engine registers itself the day it ships.
--
-- This migration is the one-time correction for what a first, buggy run of
-- the old sync_to_db() already wrote moments before this fix (label =
-- Python class name, description blank for 9 of 10 engines, because that
-- version read e.__class__.__doc__ and this codebase documents at the
-- MODULE level, not the class level -- see registry.py's own updated
-- docstring on sync_to_db() for the full trace). label/description are
-- overwritten unconditionally here -- safe because the table was empty
-- until minutes before this migration was written, so there is no operator
-- edit yet to protect; every later change to this table stays protected by
-- ON CONFLICT ... DO NOTHING at the row-insert level (sync_to_db() itself)
-- and by an operator's own Control Room edit taking precedence from here on.
--
-- Descriptions are transcribed from each engine's own module docstring —
-- not invented for this screen — matching migration 006's standard for the
-- swing side.

INSERT INTO public.intraday_strategy_config
  (strategy, enabled, lifecycle, phases, label, description)
VALUES
  ('ORB', true, 'ACTIVE', 'PRIME', 'Opening Range Breakout',
   'A decisive break of the first 15 minutes'' range, on volume — the '
   'market resolving the day''s first real disagreement. Not before 10:00 '
   '(measured 0% win before then vs 18% after, migration 134).'),

  ('GAP', true, 'ACTIVE', 'PRIME', 'Gap Up, Hold, Continue',
   'An overnight gap that holds rather than fades. NSE''s 14-hour '
   'information-dense overnight void makes a held gap the cleanest '
   'continuation signal of the session.'),

  ('PDL', true, 'ACTIVE', 'PRIME,DRIFT,AFTERNOON', 'Prev-Day High Break and Retest',
   'A break of the previous day''s high, then a retest that holds — the '
   'most-watched level in Indian intraday, traded on the retest, not the '
   'break itself.'),

  ('VCE', true, 'ACTIVE', 'PRIME,DRIFT,AFTERNOON', 'Volatility Contraction, Then Expansion',
   'An unusually quiet range releasing into a large move. Built for the '
   'cost model: a ~0.21% round trip needs roughly a 0.7% target, and this '
   'pattern''s whole premise is a big one.'),

  ('PBK', true, 'ACTIVE', 'PRIME,AFTERNOON', 'First Pullback in a Trend Day',
   'The first dip bought at a higher low in a trend day — price above VWAP '
   'all session, the stock leading its index. A tighter stop than a '
   'breakout, at a better price.'),

  ('VWR', true, 'ACTIVE', 'PRIME,DRIFT,AFTERNOON', 'VWAP Reclaim',
   'Price fades below VWAP in the midday drift, then reclaims and holds — '
   'the one pattern that works when breakout systems generate their worst '
   'signals.'),

  ('RNG', true, 'ACTIVE', 'DRIFT,AFTERNOON', 'Buy the Low of an Established Range',
   'The complement to every breakout engine here — most sessions do not '
   'break, and this is what is built for the range-bound majority.'),

  ('SDN', true, 'ACTIVE', 'PRIME,DRIFT,AFTERNOON',
   'Short Distribution (VWAP rejection, failed-breakout trap, range breakdown)',
   'Three short conditions merged into one family: supply overwhelming '
   'demand. The condition that actually fired is kept in meta.sub_engine, '
   'never pooled with a long engine of a similar name.'),

  ('GDB', true, 'ACTIVE', 'OPENING,PRIME', 'Gap-Down Bounce',
   'From a measured brain proposal, not hand-designed: 34% of gap-down '
   '>1% symbol-days produced a 3.46%+ move that no engine existing at the '
   'time caught.'),

  ('IGN', true, 'ACTIVE', 'PRIME,DRIFT,AFTERNOON', 'Ignition Momentum',
   'Violent, uncompressed acceleration in any phase, circuit-adjacent or '
   'not — built for stocks hitting an upper/lower circuit or pumping hard '
   'on volume that no other engine here can catch. ACTIVE from day one, '
   'the operator''s own explicit instruction (migration 132).')

ON CONFLICT (strategy) DO UPDATE SET
  label       = EXCLUDED.label,
  description = EXCLUDED.description,
  updated_at  = now();
