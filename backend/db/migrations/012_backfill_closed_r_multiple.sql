-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 012: derive r_multiple on historical closed trades
-- ═══════════════════════════════════════════════════════════════════════════
--
-- All 70 closed trades have r_multiple NULL. Everything that learns from
-- outcomes — the conviction model, engine scoring, expectancy, the Performance
-- tab — is therefore reasoning about percentages, which are not comparable
-- across trades. A 4% loss on a 2%-stop setup and a 4% loss on an 8%-stop setup
-- are 2.0R and 0.5R: the same number describing two completely different
-- mistakes.
--
-- WHERE THE RISK COMES FROM, IN ORDER OF PREFERENCE
--   1. planned_stop_at_entry   the stop the trade was actually sized on
--   2. the signal's planned_stop, joined on (signal_date, symbol)
--   3. nothing — left NULL
--
-- Option 3 matters. It would be easy to assume a 5% stop for the remainder and
-- fill every row, and that would be fabrication: the resulting R would describe
-- an assumption, not the trade. A NULL that means "not knowable" is worth more
-- than a number that means nothing, and the dashboard already renders NULL as
-- an em dash rather than as zero.
-- ═══════════════════════════════════════════════════════════════════════════

-- 1. Rows that already carry the stop they were sized on.
UPDATE public.closed_positions
   SET r_multiple = round(((exit_price - entry_price)
                          / NULLIF(entry_price - planned_stop_at_entry, 0))::numeric, 3)
 WHERE r_multiple IS NULL
   AND entry_price IS NOT NULL
   AND exit_price  IS NOT NULL
   AND planned_stop_at_entry IS NOT NULL
   AND planned_stop_at_entry < entry_price;

-- 2. Recover the stop from the originating signal where the trade is attributed.
UPDATE public.closed_positions AS c
   SET planned_stop_at_entry = s.planned_stop,
       planned_target_at_entry = COALESCE(c.planned_target_at_entry, s.planned_target),
       r_multiple = round(((c.exit_price - c.entry_price)
                          / NULLIF(c.entry_price - s.planned_stop, 0))::numeric, 3)
  FROM public.signal_log AS s
 WHERE c.r_multiple IS NULL
   AND c.signal_date = s.date
   AND c.symbol      = s.symbol
   AND c.entry_price IS NOT NULL
   AND c.exit_price  IS NOT NULL
   AND s.planned_stop IS NOT NULL
   AND s.planned_stop < c.entry_price;

-- 3. hold_days, where both dates exist and it is missing.
UPDATE public.closed_positions
   SET hold_days = (exit_date::date - entry_date::date)
 WHERE hold_days IS NULL
   AND entry_date IS NOT NULL
   AND exit_date  IS NOT NULL;

-- 4. MFE floor. high_water_mark was recorded on many legacy rows while
--    max_favorable_excursion was not; the peak implies the excursion.
--    Only ever raises the value, never lowers a genuinely observed one.
UPDATE public.closed_positions
   SET max_favorable_excursion = round((((high_water_mark - entry_price)
                                        / NULLIF(entry_price, 0)) * 100)::numeric, 3)
 WHERE max_favorable_excursion IS NULL
   AND high_water_mark IS NOT NULL
   AND entry_price IS NOT NULL
   AND high_water_mark > entry_price;


DO $$
DECLARE tot int; withr int; withh int; att int;
BEGIN
  SELECT count(*) INTO tot   FROM public.closed_positions;
  SELECT count(*) INTO withr FROM public.closed_positions WHERE r_multiple IS NOT NULL;
  SELECT count(*) INTO withh FROM public.closed_positions WHERE hold_days  IS NOT NULL;
  SELECT count(*) INTO att   FROM public.closed_positions WHERE signal_date IS NOT NULL;
  RAISE NOTICE 'closed_positions: % rows | % with r_multiple | % with hold_days | % attributed',
               tot, withr, withh, att;
  IF withr = 0 THEN
    RAISE NOTICE 'No R could be derived: these trades predate planned_stop being recorded.';
    RAISE NOTICE 'Trades opened from now on carry planned_stop, so R accrues going forward.';
  END IF;
END $$;
