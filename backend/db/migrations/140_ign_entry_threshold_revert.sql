-- 12-Sep-2026. Reverts migration 138's entry-threshold loosening
-- (ign_min_pct 3.5->2.2, ign_min_atr_frac 1.2->0.85), one session later.
--
-- Migration 138 shipped on a replay aggregate: n=52 trades, 31-Aug..11-Sep,
-- median +0.328R vs baseline's +0.256R. Real, but explicitly flagged at
-- the time as below this project's own n=100 sufficiency bar and "one
-- window, no holdout" -- not proof.
--
-- Asked directly to check it against one real day's own rupee P&L
-- (11-Sep, the day already under review this session) rather than only
-- the aggregate. Real result, both variants walked through the SAME
-- live exit ladder, real minute bars, real intraday.cost_model charges:
--
--   OLD (3.5 / 1.2)   6 trades   net +Rs229.43
--   NEW (2.2 / 0.85)  13 trades  net -Rs146.81
--
-- A Rs376 swing the wrong way on the one real day checked. Not narrowly
-- a short-side problem either -- checked directly: the 3 LONG trades
-- common to both variants also did worse under the looser threshold
-- that day (Rs427 old vs Rs255 new, same three symbols), so this reads
-- as genuine day-to-day variance touching both directions, not a
-- diagnosable single mechanism to patch around.
--
-- IGN stays PAPER, so nothing here ever risked capital -- but the
-- operator's own call, given the choice between "a real but thin
-- aggregate edge" and "a real, concrete daily loss," was to hold the
-- proven values rather than carry that variance while n is still this
-- low. Explicit values kept (not the rows deleted back to ignition.py's
-- own code defaults) so this revert is a visible, queryable event in
-- system_config's own history, same reasoning intraday_giveback_pct's
-- own tightening entry used. Reversible the same way, later, with more
-- real sessions behind it -- see docs/FINDINGS.md, 12-Sep-2026.

UPDATE public.system_config
SET value = '3.5',
    description = 'IGN ignition-momentum trigger, absolute floor: minimum '
        '|move| off the previous close, in percent, before magnitude+volume '
        'are even checked. Reverted to the code default (3.5) by migration '
        '140, 12-Sep-2026, after migration 138''s 2.2 was checked against a '
        'real day''s own rupee P&L (11-Sep) and found net negative (-Rs146.81 '
        'vs +Rs229.43 at the old value, both walked through the same live '
        'exit ladder) despite a positive replay AGGREGATE (n=52, still below '
        'this project''s own n=100 sufficiency bar). See ign_min_atr_frac, '
        'the other half of the same trigger formula.'
WHERE key = 'ign_min_pct';

UPDATE public.system_config
SET value = '1.2',
    description = 'IGN ignition-momentum trigger, ATR-relative floor: '
        'minimum |move| as a multiple of the stock''s own daily ATR%. '
        'Reverted to the code default (1.2) by migration 140, 12-Sep-2026 '
        '-- same real-day evidence as ign_min_pct above.'
WHERE key = 'ign_min_atr_frac';
