-- 18-Aug-2026. Six changes from the GABRIEL / HINDCOPPER study. GABRIEL was
-- entered 06-Aug at 1554.80, never once traded above its entry price (peak
-- +0.03%, high-water mark 1555.20 against a 1554.80 fill), and was closed
-- 17-Aug at 1432.60 for -7.86% -- BY HAND. SCI is on the same path today:
-- entered 10-Aug at 300.60, peak 301.60, currently -4.36% and -0.61R.
--
-- Every gate needed to tell GABRIEL from HINDCOPPER already existed. Three were
-- inert and one was mathematically incapable of firing. These keys switch them
-- on and add the two rules that were genuinely missing.
--
-- 1. EXIT_FASTFAIL. The exit ladder had a ten-session gap. Every profit-side
--    rule (target, partial, breakeven trail, give-back) is unreachable on a
--    trade that is never profitable; EXIT_DETERIORATION is gated at +1.0R;
--    the stop was 9.7% away. So nothing could see GABRIEL until the stall rule
--    on day ten. On 10-Aug -- four sessions in -- it was -5.5% and -0.56R with a
--    peak of 0.00R: every term below was already true.
--
--    OFF BY DEFAULT. This is the only rule in the ladder that sells while the
--    ordinary stop is still far away, so a miscalibration costs real money
--    rather than a missed opportunity. Turn it on deliberately.
--
-- 2. LIQUIDITY FLOOR. GABRIEL traded Rs 123 cr on its best day and Rs 17 cr by
--    14-Aug; HINDCOPPER traded Rs 816 cr. Thin names gap through stops and fill
--    badly -- which is exactly what happened on the 17-Aug exit, where a limit
--    sat unfilled for 2h33m. 200 is a floor for a Rs 30,000 book, not a view on
--    the companies.
--
-- 3. FROZEN PLAN LEVELS. planned_stop tracked the price up: 1248.49 on 29-Jul
--    (source 'structure'), 1403.97 by 05-Aug (source 'atr'). risk_model.py's
--    own docstring says the stop must NOT move up with price, because that is
--    what makes min_rr a chase penalty. With both legs as ATR multiples of the
--    current price, implied_rr is pinned at exactly 0.777 -- it read 0.777 on
--    three consecutive GABRIEL rows across a 4% price range, and on HINDCOPPER
--    too. A gate comparing a constant against a threshold does not discriminate.
--    GABRIEL was finally admitted on 06-Aug when the source flipped back to
--    'structure' and the ratio landed at 0.805, clearing the 0.80 NEUTRAL bar
--    by five thousandths.
--
-- 4. AI REFUSALS CARRY WEIGHT. The 03-Aug review returned
--    eap_action = 'AVOID_ENTRY' and the 29-Jul one warned "Extended RSvN could
--    lead to mean reversion". Both are stored; neither reached entry_ranking.
--
-- 5. EXIT EXECUTION TERMINATES. The 17-Aug exit placed a SELL LIMIT at
--    ltp * (1 - 30bps) = 1460.60 on a stock with 4.7% daily ATR, cancelled the
--    protective GTT 48 seconds later, and then left the order resting above a
--    falling market. It was still unfilled 2h33m afterwards when the operator
--    repriced it by hand. Without that intervention the position would still be
--    open with no stop of any kind.

INSERT INTO system_config (key, value, description) VALUES
  ('exit_fastfail_enabled', 'false',
   'Cut a swing position that has never worked. OFF by default: it sells while the ordinary stop is still far away.'),
  ('exit_fastfail_days', '4',
   'Sessions held before EXIT_FASTFAIL may fire. Fewer than four is inside the normal wobble of an entry.'),
  ('exit_fastfail_peak_r', '0.25',
   'Peak favourable excursion below which a position counts as never having worked. Separates never-worked from worked-and-gave-it-back.'),
  ('exit_fastfail_gain_r', '-0.5',
   'How far underwater EXIT_FASTFAIL requires. A flat trade going nowhere is left to the 10-session stall rule.'),

  ('swing_min_value_cr', '200',
   'Minimum daily traded value in Rs crore for a swing entry. GABRIEL Rs 123 cr, HINDCOPPER Rs 816 cr.'),
  ('swing_liquidity_floor_enabled', 'true',
   'Enforce swing_min_value_cr at the entry gate.'),

  ('plan_levels_frozen', 'true',
   'planned_stop/planned_target are set once when a signal is born and never recomputed while it lives. This is what makes min_rr_to_enter a real chase penalty.'),

  ('entry_rank_respect_ai_avoid', 'true',
   'entry_ranking refuses a plan whose AI review returned eap_action = AVOID_ENTRY.'),
  ('entry_rank_ai_avoid_penalty', '25',
   'Rank points removed when the AI review flags a risk short of AVOID_ENTRY. Annotation, never promotion.'),

  ('exit_order_reprice_after_s', '60',
   'Seconds an unfilled SELL exit may rest before it is repriced to the current market.'),
  ('exit_order_max_repricings', '3',
   'Repricings before the exit is sent as MARKET. An exit that does not terminate is not an exit.'),
  ('exit_order_stale_alert_s', '300',
   'tools.health FAILs when any SELL order has been open longer than this.'),
  ('exit_order_reprice_enabled', 'true',
   'Walk an unfilled SELL to the market, MARKET after exit_order_max_repricings. ON: the alternative is the 17-Aug state, a decided exit resting unfilled with its GTT already cancelled.')
ON CONFLICT (key) DO NOTHING;

-- The exit slippage buffer is a floor now, not the whole allowance: the order
-- manager takes max(exit_slip_bps, exit_slip_atr_frac * atr_pct). 30bps on a
-- 4.7% ATR stock is a limit that will not fill in the move that triggered it.
INSERT INTO system_config (key, value, description) VALUES
  ('exit_slip_atr_frac', '0.25',
   'Fraction of daily ATR%% used as the exit limit buffer when it exceeds exit_slip_bps.')
ON CONFLICT (key) DO NOTHING;
