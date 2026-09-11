-- 12-Sep-2026. Replay-tested whether IGN's entry trigger fires too late
-- relative to what its own detected moves go on to do. Built
-- tools/replay/ign_entry_exit_variant_check.py: 75 trade-rows, real minute
-- bars, 31-Aug..11-Sep (the fully reachable window at run time), two
-- independent levers tested as a 2x2 (entry threshold x exit looseness).
--
-- ENTER EARLIER (this migration): ign_min_pct 3.5->2.2, ign_min_atr_frac
-- 1.2->0.85. Both keys were running purely on ignition.py's own code
-- defaults -- neither had a system_config row before this one. Looser
-- thresholds more than doubled the detected trade count (23->52 in the
-- replayed window) AND improved both median R (+0.256->+0.328) and win
-- rate (56.5%->65.4%) -- an improvement on both axes, not a tradeoff.
--
-- Real evidence, not proof: n=52 is still below this project's own n=100
-- sufficiency bar, one window, no holdout split. Shipped anyway, same
-- precedent as IGN's own 08-Sep launch (shipped ACTIVE on zero scored
-- outcomes, migration 132) -- IGN stays PAPER regardless, so the cost of
-- being wrong here is more noisy paper trades, not capital, and every one
-- it now takes becomes real evidence for the next look. Full replay output
-- in docs/FINDINGS.md, 12-Sep-2026.
--
-- NOT SHIPPED: the "stay in longer" lever (giveback_pct 30->65 plus an
-- effectively-removed target) tested in the SAME run and found to CUT
-- median R roughly in half on both entry variants (+0.256->+0.094,
-- +0.328->+0.115), consistently, not a one-stock fluke. One trade
-- (PINELABS, 11-Sep) showed +3.50R under that variant and is deliberately
-- NOT the reason to act on it -- the exact "one big winner distorts the
-- aggregate" trap this ledger already warns about elsewhere (the earlier
-- gb=off finding). No config change from that half of the test.
--
-- ON CONFLICT DO NOTHING, matching every other IGN-specific key's own
-- convention (migration 133) -- never silently re-armed by a re-run, and
-- the operator may deliberately retune either value later.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('ign_min_pct', '2.2',
   'IGN ignition-momentum trigger, absolute floor: minimum |move| off the '
   'previous close, in percent, before magnitude+volume are even checked. '
   'Ran on ignition.py''s own code default (3.5) with no system_config row '
   'until this migration. Replay-tested (docs/FINDINGS.md, 12-Sep-2026): '
   'loosening to 2.2 more than doubled detected trades (23->52 over '
   '31-Aug..11-Sep) and improved both median R and win rate. See '
   'ign_min_atr_frac, the other half of the same trigger formula '
   '(min_move = max(this, atr_pct_daily * ign_min_atr_frac)).',
   'Intraday engines', 'intraday/strategies/ignition.py', 'float', '3.5', 'MEDIUM'),
  ('ign_min_atr_frac', '0.85',
   'IGN ignition-momentum trigger, ATR-relative floor: minimum |move| as a '
   'multiple of the stock''s own daily ATR%. Ran on ignition.py''s own code '
   'default (1.2) with no system_config row until this migration -- same '
   'replay evidence as ign_min_pct above.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'float', '1.2', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
