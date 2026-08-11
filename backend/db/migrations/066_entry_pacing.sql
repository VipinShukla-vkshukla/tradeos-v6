-- 11-Aug-2026. Both entry budgets (swing_max_new_per_day, migration-era;
-- intraday_max_new_per_day, 31-Jul) count entries but never paced them. On
-- 10-Aug three swing entries fired 09:36:12, 09:36:24, 09:37:03 -- 51
-- seconds apart -- because a count-only check reads 0/3, then 1/3, then
-- 2/3 and calls each one "budget available". The whole day's swing risk
-- was committed in under a minute, at the open, on a ranking built from
-- the least reliable minutes of the session (see 0_SYSTEM_BLUEPRINT.md /
-- the 11-Aug institutional review), with nothing left for anything better
-- that showed up at noon or in the afternoon.
--
-- execution/order_manager.py::_last_entry_at() (SWING, sourced from
-- intraday_broker_log, the LIVE order audit trail) and
-- intraday/engine.py::IntradayEngine._last_intraday_entry_at() (INTRADAY,
-- sourced from intraday_setups.ts, since intraday runs in PAPER and paper
-- fills do not write to the broker log) now answer "how recently", and
-- _maybe_enter_swing() / _maybe_open_paper() refuse an entry that arrives
-- before its book's minimum gap has elapsed since the last one -- the
-- COUNT still governs how many, this only governs how soon.
--
-- Both are rollback levers set to 0 restores exactly today's (count-only)
-- behaviour. The FIRST entry of the day is never gated by either -- there
-- is no "too soon after" a last entry that does not exist yet.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('swing_entry_min_gap_minutes', '20.0',
   'Minimum wall-clock minutes between two swing entries taken by this '
   'process today. 0 disables pacing and restores count-only budget '
   'enforcement (today''s pre-11-Aug behaviour). See '
   'execution/order_manager.py::_last_entry_at and '
   'intraday/engine.py::_maybe_enter_swing.',
   'Master controls', 'intraday/engine.py', 'float', '20.0', 'MEDIUM'),
  ('intraday_entry_min_gap_minutes', '5.0',
   'Minimum wall-clock minutes between two intraday entries taken today. '
   'Tighter than swing''s default (5 vs 20) because intraday setups are '
   'more time-sensitive -- several names clearing their gates in the same '
   '15s cycle are more likely one correlated market move than independent '
   'opportunities. 0 disables pacing. See '
   'intraday/engine.py::_last_intraday_entry_at and ::_maybe_open_paper.',
   'Master controls', 'intraday/engine.py', 'float', '5.0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
