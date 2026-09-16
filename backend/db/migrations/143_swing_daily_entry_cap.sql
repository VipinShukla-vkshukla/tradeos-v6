-- 17-Sep-2026. Swing daily entry cap 10 -> 3.
--
-- Raised to 10 on 25-Aug-2026 with the move to a Rs 3,00,000 paper book. On
-- 26-Aug the book opened 9 positions in one session into a market that fell
-- ~5% over the next two weeks; they closed 33% winners, -Rs 3,225.
--
-- Replay (tools.swing_fix_replay, on top of the regime fix and migration 142):
--   cap   recent (entered since 14-Aug)   earlier (13-Jul..13-Aug)
--    2        +3,970 / +3.03R                  -65
--    3        +3,014 / +2.26R                    0   <- proposed before the replay
--    4          +745 / +0.89R                    0
--    5        +1,244 / +1.22R                    0
-- Most of the effect is the single 26-Aug session. 3 is the value proposed in the
-- review before any replay ran, and costs nothing in the earlier window.

UPDATE public.system_config
   SET value = '3'
 WHERE key = 'swing_max_new_per_day'
   AND value = '10';
