-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 039: give the results calendar a memory (Stage 8)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE POST-EARNINGS DRIFT ENGINE NEEDS A PAST. THE SYSTEM ONLY HAS A FUTURE.
--
-- Every planning document lists PEAD as the cheapest new alpha available
-- because "every input it requires already flows": results dates, gaps,
-- delivery, volume. Three of those four are right.
--
-- nifty_upcoming_events is, as its name says, UPCOMING. Measured 04-Aug-2026:
-- 767 results events spanning 2026-08-03 to 2026-08-18 — one day of past and
-- two weeks of future. Rows are refreshed (created_at spans 22-Jun to 03-Aug)
-- and dates that pass simply stop being there.
--
-- PEAD's entire thesis is "this name reported N days ago and is still
-- drifting". A calendar that cannot say what reported last Tuesday cannot
-- support it. Built against that table the engine returned 0 candidates from
-- 165 recent results, and would have kept returning 0 — not because the
-- anomaly is absent but because the question was unanswerable.
--
-- THE FIX IS SMALL, AND IT WORKS IMMEDIATELY RATHER THAN IN A QUARTER.
--
-- The forward calendar already knows about 2026-08-18. Capture what it says
-- ONCE and the future becomes the past on its own as the dates arrive. So this
-- is not "start recording and wait" — seeding from today's table gives PEAD a
-- real fifteen-day window the moment it is applied, and each nightly run
-- extends the far end.
--
-- Upserted on (symbol, event_date) so re-running the pipeline cannot duplicate
-- a company's results day, and so a date that shifts is corrected rather than
-- doubled.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.results_calendar (
    symbol      text NOT NULL,
    event_date  date NOT NULL,
    purpose     text,
    company_name text,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, event_date)
);

CREATE INDEX IF NOT EXISTS idx_results_calendar_date
    ON public.results_calendar (event_date DESC);

COMMENT ON TABLE public.results_calendar IS
  'Persistent record of company results dates. nifty_upcoming_events holds only '
  'what is coming and drops what has passed; PEAD needs to know what reported '
  'last week. Captured nightly from the same feed, upserted so a re-run cannot '
  'duplicate a results day.';

-- Seed from whatever the forward calendar currently knows, so the engine has a
-- usable window the day this is applied rather than a quarter later.
INSERT INTO public.results_calendar (symbol, event_date, purpose, company_name)
SELECT DISTINCT ON (symbol, event_date)
       upper(symbol), event_date::date, purpose, company_name
  FROM public.nifty_upcoming_events
 WHERE purpose ILIKE '%result%'
   AND symbol IS NOT NULL
   AND event_date IS NOT NULL
ON CONFLICT (symbol, event_date) DO NOTHING;

DO $$
DECLARE n integer; lo date; hi date;
BEGIN
  SELECT count(*), min(event_date), max(event_date)
    INTO n, lo, hi FROM public.results_calendar;
  RAISE NOTICE '';
  RAISE NOTICE 'results_calendar seeded: % results day(s), % to %.', n, lo, hi;
  RAISE NOTICE '';
  RAISE NOTICE 'PEAD can now ask "what reported N days ago". Until the seeded';
  RAISE NOTICE 'window''s dates actually pass, it will find few or no candidates —';
  RAISE NOTICE 'that is the calendar being honest, not the engine being broken.';
END $$;


-- ── Register the two Stage 8 engines, both SHADOW ──────────────────────────
--
-- Neither receives capital until it has shadowed at detection level for a full
-- quarter and shown independent expectancy. screen_stocks already routes SHADOW
-- engines' hits into engines_list (so screener_performance attributes forward
-- returns to them) while keeping their scores out of composite_score. That
-- mechanism is what makes adding an engine safe, and it already works.
--
-- A new engine going live on its author's confidence is precisely the failure
-- the governance stage exists to prevent, and these two are the author's.
-- strategy_config.params is NOT NULL and carries a CHECK on its shape (migration
-- 005, after a double-encoded string turned params into an ARRAY and took two
-- fatal pipeline steps down with it). The first version of this migration
-- omitted it, and because the SQL editor runs the file as one transaction the
-- whole migration rolled back — including the table above. Gates are supplied
-- in the same grammar engine_registry.evaluate_gates already reads, so these
-- two are visible and tunable from the Control Room like every other engine.
INSERT INTO public.strategy_config
  (strategy, enabled, lifecycle, label, description, engine_type, params)
VALUES
  ('PEAD', true, 'SHADOW', 'Post-earnings drift',
   'Results-day reaction, delivery-confirmed, held for the drift rather than '
   'faded. Edge is structural: a large buyer cannot compress a week of '
   'accumulation into one session, so the drift is a consequence of size rather '
   'than a pattern to be arbitraged away.',
   'SCANNER',
   '{"lifecycle": "SHADOW",
     "source": "STAGE8",
     "gates": {
       "delivery_pct": {"min": 45},
       "vol_ratio":    {"min": 1.5},
       "above_sma50":  {"eq": true},
       "rsi_daily":    {"max": 80},
       "pct_change":   {"min": 2.0, "max": 12.0}
     }}'::jsonb),

  ('ACC',  true, 'SHADOW', 'Accumulation-confirmed',
   'Delivery-% persistence as the PRIMARY sort, structure only as confirmation. '
   'The inversion is the point: the breakout level is the crowded half, and '
   'sustained delivery elevation is what the crowd cannot see. Block and bulk '
   'deals corroborate; they never create the candidate.',
   'SCANNER',
   '{"lifecycle": "SHADOW",
     "source": "STAGE8",
     "gates": {
       "delivery_pct": {"min": 55},
       "market_cap":   {"min": 300}
     }}'::jsonb)
ON CONFLICT (strategy) DO NOTHING;

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('pead_window_days', '5',
   'Sessions after a results day during which the drift is still tradeable. '
   'Day 0 is excluded — the results bar is the reaction, not the drift.',
   'Screening', 'swing/signals/engines_stage8.py', 'int', '5', 'TUNING'),
  ('pead_min_gap_pct', '2.0',
   'Minimum results-day move for the reaction to count as real.',
   'Screening', 'swing/signals/engines_stage8.py', 'float', '2.0', 'TUNING'),
  ('pead_max_gap_pct', '12.0',
   'Above this the market has repriced rather than under-reacted, and there is '
   'no drift left to capture.',
   'Screening', 'swing/signals/engines_stage8.py', 'float', '12.0', 'TUNING'),
  ('pead_min_delivery_pct', '45.0',
   'Delivery on the RESULTS DAY. This is the confirmation leg and it is not '
   'optional: a results gap on low delivery is speculative repositioning that '
   'unwinds, and without this the engine is just "buy things that gapped up".',
   'Screening', 'swing/signals/engines_stage8.py', 'float', '45.0', 'CRITICAL'),
  ('acc_min_elevated_days', '8',
   'How many of the last acc_lookback_sessions must show elevated delivery. One '
   'high-delivery day is a block trade; persistence is what a buyer working an '
   'order over weeks leaves behind.',
   'Screening', 'swing/signals/engines_stage8.py', 'int', '8', 'CRITICAL'),
  ('acc_elevated_delivery_pct', '55.0',
   'Delivery level counted as elevated. Also checked against the name''s own '
   'median, because some names simply trade at high delivery and that is a '
   'property of the register rather than evidence anything changed.',
   'Screening', 'swing/signals/engines_stage8.py', 'float', '55.0', 'TUNING')
ON CONFLICT (key) DO NOTHING;
