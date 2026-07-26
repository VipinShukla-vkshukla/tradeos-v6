-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 003: position lifecycle + outcome attribution
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
--   Nothing in the running system wrote open_positions or closed_positions.
--   Verified against production:
--     open_positions   : 2 rows, with active_sl, current_price, target_price,
--                        high_water_mark, trailing_sl_pct, signal_id,
--                        original_qty, current_qty, unrealized_pnl, pnl_pct
--                        and lifecycle ALL NULL on every row
--     closed_positions : 70 rows, with signal_date NULL on all 70 and
--                        entry_timing_type NULL on all 70
--
--   The three candidate writers were all non-functional:
--     brain/position_manager.py   — wrote columns that do not exist (id,
--                                   entry_price_actual, stop_loss,
--                                   regime_at_entry, telegram_msg_id,
--                                   open_position_id, quantity), used
--                                   status='OPEN'/'CLOSED' while every reader
--                                   filters status='ACTIVE', and read
--                                   signal_log.current_price which was not a
--                                   column. No webhook receiver ever existed,
--                                   so its Telegram buttons were inert.
--     control/execution_engine.py — ImportError at load (get_config,
--                                   get_config_int, check_kill_switch,
--                                   kite.kite_client all absent)
--     control/sl_monitor.py       — imported the missing kite.kite_client, so
--                                   fetch_live_prices always returned {} and
--                                   the function returned before checking a
--                                   single stop
--
--   Consequence: post_trade_analysis, performance_tracker, ml_provider and the
--   whole Brain learn exclusively from closed_positions. With signal_date NULL
--   on every row, no outcome could ever be attributed back to the signal that
--   produced it — signal_outcomes holds 2 rows, last written 2026-06-29.
--
-- Additive only. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── open_positions ─────────────────────────────────────────────────────────
ALTER TABLE public.open_positions
    -- Trade plan captured at entry, copied from the signal. Frozen: these are
    -- what the setup promised, so post-trade analysis can compare plan to
    -- outcome. active_sl moves; planned_stop does not.
    ADD COLUMN IF NOT EXISTS planned_stop            numeric,
    ADD COLUMN IF NOT EXISTS planned_target          numeric,
    ADD COLUMN IF NOT EXISTS planned_risk_pct        numeric,
    ADD COLUMN IF NOT EXISTS expected_r_at_entry     numeric,
    ADD COLUMN IF NOT EXISTS entry_signal_type       text,
    ADD COLUMN IF NOT EXISTS entry_timing_type       text,
    ADD COLUMN IF NOT EXISTS lifecycle_at_entry      text,
    ADD COLUMN IF NOT EXISTS regime_at_entry         text,
    ADD COLUMN IF NOT EXISTS sector_rank_at_entry    integer,
    -- Exit-policy state machine
    ADD COLUMN IF NOT EXISTS partial_booked_qty      integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS partial_booked_price    numeric,
    ADD COLUMN IF NOT EXISTS partial_booked_at       timestamptz,
    ADD COLUMN IF NOT EXISTS breakeven_moved         boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS trail_activated         boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS time_stop_date          date,
    -- Excursion tracking. max_favorable_excursion already exists on
    -- closed_positions but was never populated; tracking it live is the only
    -- way to measure capture ratio (realised P&L / best unrealised gain).
    ADD COLUMN IF NOT EXISTS max_favorable_excursion numeric,
    ADD COLUMN IF NOT EXISTS max_adverse_excursion   numeric,
    ADD COLUMN IF NOT EXISTS r_multiple_current      numeric,
    -- Provenance + reconciliation
    ADD COLUMN IF NOT EXISTS source                  text DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS kite_avg_price          numeric,
    ADD COLUMN IF NOT EXISTS notes                   text,
    ADD COLUMN IF NOT EXISTS created_at              timestamptz DEFAULT now();

COMMENT ON COLUMN public.open_positions.source IS
    'kite | telegram | manual — how the row was created. Kite reconciliation is '
    'authoritative and overwrites quantity/average price on conflict; telegram '
    'rows are provisional until Kite confirms them.';
COMMENT ON COLUMN public.open_positions.planned_stop IS
    'Stop the SIGNAL was validated against (analysis/risk_model). Never moves. '
    'active_sl starts here and is ratcheted upward by the exit policy.';
COMMENT ON COLUMN public.open_positions.max_favorable_excursion IS
    'Best unrealised gain %% seen while open. Realised/MFE is the capture ratio; '
    'measured at 3%% median on the first 70 closed trades.';


-- ── closed_positions ───────────────────────────────────────────────────────
ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS signal_id               bigint,
    ADD COLUMN IF NOT EXISTS entry_signal_type       text,
    ADD COLUMN IF NOT EXISTS regime_at_entry         text,
    ADD COLUMN IF NOT EXISTS sector_rank_at_entry    integer,
    ADD COLUMN IF NOT EXISTS planned_stop_at_entry   numeric,
    ADD COLUMN IF NOT EXISTS planned_target_at_entry numeric,
    ADD COLUMN IF NOT EXISTS hold_days               integer,
    ADD COLUMN IF NOT EXISTS r_multiple              numeric,
    ADD COLUMN IF NOT EXISTS max_adverse_excursion   numeric,
    ADD COLUMN IF NOT EXISTS exit_reason_detail      text,
    ADD COLUMN IF NOT EXISTS partial_bookings        jsonb,
    ADD COLUMN IF NOT EXISTS source                  text DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS closed_at               timestamptz DEFAULT now();

COMMENT ON COLUMN public.closed_positions.r_multiple IS
    'Realised P&L expressed in units of the risk taken at entry: '
    '(exit - entry) / (entry - planned_stop_at_entry). The only P&L measure '
    'comparable across a 3%%-stop trade and a 7%%-stop trade, and the correct '
    'input to expectancy arithmetic.';
COMMENT ON COLUMN public.closed_positions.signal_id IS
    'FK to signal_log.id. NULL on all 70 pre-migration rows, which is why '
    'signal_outcomes could never be populated and the Brain had nothing to '
    'learn from.';

CREATE INDEX IF NOT EXISTS closed_positions_signal_idx
    ON public.closed_positions (signal_id);
CREATE INDEX IF NOT EXISTS closed_positions_entry_date_idx
    ON public.closed_positions (entry_date);
CREATE UNIQUE INDEX IF NOT EXISTS open_positions_symbol_active_uidx
    ON public.open_positions (symbol) WHERE status = 'ACTIVE';


-- ── Broker reconciliation audit trail ──────────────────────────────────────
-- Every divergence between Kite and open_positions is recorded rather than
-- silently corrected, so an unexpected fill or a manual trade outside the
-- framework is visible instead of being absorbed.
CREATE TABLE IF NOT EXISTS public.position_reconcile_log (
    id            bigserial PRIMARY KEY,
    run_at        timestamptz DEFAULT now() NOT NULL,
    trade_date    date        NOT NULL,
    symbol        text        NOT NULL,
    action        text        NOT NULL,   -- OPENED | CLOSED | QTY_INCREASED | QTY_REDUCED | SL_UPDATED | DRIFT | ORPHAN
    db_qty        integer,
    broker_qty    integer,
    db_price      numeric,
    broker_price  numeric,
    detail        text,
    resolved      boolean DEFAULT true
);
CREATE INDEX IF NOT EXISTS position_reconcile_log_date_idx
    ON public.position_reconcile_log (trade_date DESC);

ALTER TABLE public.position_reconcile_log OWNER TO postgres;
GRANT ALL ON TABLE public.position_reconcile_log TO anon;
GRANT ALL ON TABLE public.position_reconcile_log TO authenticated;
GRANT ALL ON TABLE public.position_reconcile_log TO service_role;
GRANT ALL ON SEQUENCE public.position_reconcile_log_id_seq TO anon;
GRANT ALL ON SEQUENCE public.position_reconcile_log_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.position_reconcile_log_id_seq TO service_role;


-- ── Exit policy parameters ─────────────────────────────────────────────────
-- Values chosen from a simulation over the 70 realised trades. On the subset
-- whose entries actually worked (MFE >= +2% within 5 sessions, n=44), the
-- measured expectancy was:
--     actual policy                            +1.26%
--     6% stop, book 50% @ +6%, target +12%     +1.50%
-- and on trades that reached +5% (n=38): actual +1.77% vs +2.81%.
-- Expressed in R rather than fixed percentages so the rule adapts to each
-- stock's volatility instead of assuming every trade has the same ATR.
INSERT INTO public.system_config (key, value, description) VALUES
  ('exit_partial_book_r',    '1.5',
   'Book partial profit when unrealised gain reaches this multiple of initial risk. 1.5R on a 4%% stop is +6%%.'),
  ('exit_partial_book_pct',  '50',
   'Percent of the position to book at exit_partial_book_r.'),
  ('exit_move_to_breakeven', 'true',
   'Move the stop to entry once the partial booking is done. Converts the trade to a free option.'),
  ('exit_trail_r',           '1.5',
   'Trailing stop distance below the high-water mark, in multiples of initial risk. Expressed in R rather than percent: a percentage trail does not compose with an R-based target (with a 5%% stop and a 3R target, a 10%% trail never rises above the breakeven stop before the target fires, so the trailing branch was unreachable) and is also backwards — it trails tighter on volatile stocks than on calm ones.'),
  ('exit_trail_after_r',     '2.0',
   'Do not trail until unrealised gain reaches this multiple of R. Trailing from day 1 cut the average winner to +3.89%%.'),
  ('exit_target_r',          '3.0',
   'Hard target as a multiple of initial risk. Matches risk_target_atr_mult / risk_stop_atr_mult = 3.0/1.5 = 2R plus room.'),
  ('exit_time_stop_days',    '15',
   'Close a position that has gone nowhere after this many SESSIONS. Median day of peak MFE was 11, so 15 gives the thesis room without dead money.'),
  ('exit_time_stop_min_r',   '0.5',
   'Time stop only fires when the position is below this R multiple — a trade that is working is left alone.'),
  ('position_reconcile_enabled', 'true',
   'Master switch for Kite -> open_positions reconciliation.'),
  ('position_auto_close',    'true',
   'When a holding disappears from Kite, automatically write the closed_positions row.')
ON CONFLICT (key) DO NOTHING;
