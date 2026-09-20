-- 20-Sep-2026. Cut losers faster — the one behaviour change in this series that
-- is positive in BOTH windows on the book's own real trades.
--
-- Replayed through current mechanics on cached 15-minute bars, 51 real swing
-- trades, with the live exit ladder:
--
--   window                    current                    faster
--   earlier (n=16)   62.5% win  +2.48R   +169     56.2% win  +2.81R   +238
--   recent  (n=34)   38.2% win  -9.03R  -9,664    32.4% win  -7.87R  -8,586
--
-- maxDD improves in both (-350 -> -234, -11,161 -> -9,203). The win rate falls
-- ~6 points in both windows: that is the agreed objective (money per trade, not
-- win count) and it should be expected on screen.
--
-- OUTLIER GATE, the PAYTM-illusion check: dropping the two trades contributing
-- most to the improvement makes it BIGGER, not smaller — recent +1,078 becomes
-- +3,401 without CARTRADE (-1,408) and MAHABANK (-915), both of which work
-- against the change. The gain is broad-based, not two lucky trades.
--
-- At book level (simulated selection, live caps, holdout) the same settings
-- improve R per trade -0.243 -> -0.101 and maxDD -56,876 -> -52,779, while
-- rupees are flat because freeing capital sooner buys more trades (41 -> 55).
-- Less bleed per trade and a smaller hole; not a proven rupee gain.
--
-- evaluate_exit() already reads all three keys. No code change. Revert is this
-- file with the values swapped.

UPDATE public.system_config SET value = '3'     WHERE key = 'exit_fastfail_days'   AND value = '4';
UPDATE public.system_config SET value = '-0.35' WHERE key = 'exit_fastfail_gain_r' AND value = '-0.5';
UPDATE public.system_config SET value = '6'     WHERE key = 'exit_stall_days'      AND value = '10';
