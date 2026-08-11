-- 11-Aug-2026. The token had been stale since Friday 04:33 IST and nothing
-- surfaced that until an operator happened to read the log after the fact:
-- the daemon logged "no prices yet" as a routine WARNING for 21 minutes at
-- the 10-Aug open, indistinguishable in the log from ordinary startup
-- noise. kite/token_manager.py::alert_if_stale() now sends a Telegram
-- push instead, both proactively (--check-and-alert, meant for a scheduled
-- pre-market run -- see tradeos.cmd token-check) and reactively
-- (intraday/run.py, once a stretch of blindness crosses this threshold).
--
-- This module still does NOT automate the Zerodha login itself -- storing
-- a password and TOTP seed to drive it would hand an unattended script
-- full trading authority, the same reasoning the module's docstring has
-- always given. This closes the other half: making sure the operator is
-- TOLD, promptly, rather than discovering it by chance.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_token_alert_after_cycles', '4',
   'Consecutive blind (no-price) 15s cycles before intraday/run.py sends a '
   'Telegram alert via kite.token_manager.alert_if_stale(). Deliberately '
   'tighter than intraday_blind_cycles_before_standdown (8) so the operator '
   'is notified before the daemon starts shuffling leases between '
   'instances. The alert itself no-ops silently if the token is actually '
   'valid -- a websocket blip with a fine token sends nothing.',
   'Master controls', 'intraday/run.py', 'int', '4', 'LOW')
ON CONFLICT (key) DO NOTHING;
