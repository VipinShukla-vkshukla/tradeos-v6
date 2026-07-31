-- 029  signal_log outcome columns
--
-- post_trade_analysis.enrich_signal_log() writes four columns back to
-- signal_log to close a signal's lifecycle. Only one of them existed.
--
--     ai_note           present
--     outcome           MISSING
--     outcome_pnl_pct   MISSING
--     execution_status  MISSING
--
-- PostgREST rejects the whole UPDATE when any column is unknown, so the write
-- failed atomically: on 2026-07-31 it reported "0/16 signal_log enriched" and
-- sixteen PGRST204 warnings. The failure is silent in the sense that matters —
-- the step is marked OK and the pipeline summary shows 27/27 green, while every
-- closed trade's outcome fails to reach the table the learning loop reads.
--
-- This is the same class as the four health checks that reported green while
-- the thing they watched was broken: an operation whose failure changes nothing
-- visible until someone asks the data a question it cannot answer.
--
-- Safe on a live book: three nullable columns, no default backfill, no
-- constraint. Existing rows read NULL, which is what they honestly are — the
-- outcome was never recorded for them.

ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS outcome          TEXT,
    ADD COLUMN IF NOT EXISTS outcome_pnl_pct  NUMERIC,
    ADD COLUMN IF NOT EXISTS execution_status TEXT;

COMMENT ON COLUMN signal_log.outcome IS
    'WIN or LOSS, written by post_trade_analysis when the position closes.';
COMMENT ON COLUMN signal_log.outcome_pnl_pct IS
    'Realised P&L percent for the trade this signal produced.';
COMMENT ON COLUMN signal_log.execution_status IS
    'COMPLETED once the signal has produced a closed trade and been scored.';

-- Finding these rows is the whole point of the columns, and a partial index
-- costs nothing on the rows that are still NULL.
CREATE INDEX IF NOT EXISTS idx_signal_log_execution_status
    ON signal_log (execution_status)
    WHERE execution_status IS NOT NULL;
