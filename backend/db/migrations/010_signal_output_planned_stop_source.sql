-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 010: planned_stop_source on signal_output_daily
-- ═══════════════════════════════════════════════════════════════════════════
--
-- signals/final_snapshot.py builds a 114-key row and upserts it into
-- signal_output_daily. 113 of those keys are real columns. planned_stop_source
-- is not, so the next run of step 20.5 raises
--
--     PGRST 42703: column signal_output_daily.planned_stop_source does not exist
--
-- and step 20.5 is fatal — the whole evening pipeline stops before alerts are
-- built. It has not fired yet only because the last successful snapshot ran
-- before that key was added to the payload.
--
-- WHY THE COLUMN IS WANTED, NOT JUST THE CRASH FIXED
-- --------------------------------------------------
-- analysis/trade_decision.decide() reads planned_stop_source to distinguish two
-- very different situations that otherwise look identical:
--
--   "no plan because the data is missing"      -> a pipeline problem
--   "no plan because compute_trade_levels
--    DECLINED to size this trade"              -> the risk model working
--
-- The second case carries a reason such as risk_too_wide_8.2pct, meaning the
-- ATR-derived stop sat further below entry than the risk cap allows. decide()
-- turns that into "SKIP — stop would sit 8.2% below entry, past the risk cap"
-- instead of the unhelpful "no decision — missing stop, target".
--
-- send_alerts.py reads signal_output_daily, not signal_log. Without this column
-- that explanation can never reach a Telegram message or the dashboard, so the
-- most common legitimate reason for a missing plan stays invisible.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.signal_output_daily
    ADD COLUMN IF NOT EXISTS planned_stop_source text;

COMMENT ON COLUMN public.signal_output_daily.planned_stop_source IS
  'How planned_stop was derived, or why it was not: atr | structure | '
  'atr_capped | risk_too_wide_<pct>pct | no_atr. Read by '
  'analysis/trade_decision.decide() to explain a SKIP. Mirrors the column of '
  'the same name on signal_log.';

-- Backfill from signal_log where the pair is already known, so existing rows
-- carry the reason instead of starting NULL.
UPDATE public.signal_output_daily AS o
   SET planned_stop_source = l.planned_stop_source
  FROM public.signal_log AS l
 WHERE o.date = l.date
   AND o.symbol = l.symbol
   AND o.planned_stop_source IS NULL
   AND l.planned_stop_source IS NOT NULL;

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.signal_output_daily
   WHERE planned_stop_source IS NOT NULL;
  RAISE NOTICE 'planned_stop_source populated on % rows', n;
END $$;
