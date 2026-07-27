-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 011: intraday subsystem
-- ═══════════════════════════════════════════════════════════════════════════
--
-- STORAGE IS THE BINDING CONSTRAINT, SO IT IS THE DESIGN INPUT
-- ------------------------------------------------------------
-- Supabase free tier is 500 MB. A naive intraday design fills that in days:
-- streaming 40 symbols at roughly one tick per second over a 6.25-hour session
-- is about 900,000 ticks per day. At ~100 bytes a row that is 90 MB PER DAY —
-- the whole quota gone inside a working week, for data nobody will ever query.
--
-- So the rule for this subsystem is: STORE DECISIONS, NEVER OBSERVATIONS.
--
--   ticks           held in memory only, never written
--   evaluations     ~1,500 per position per day, never written
--   NOTIFIED alerts written — a state CHANGE, a few dozen rows a day
--   broker writes   written — every GTT and order, success or failure
--
-- That is roughly 50-200 rows a day, under 100 KB a month. Two orders of
-- magnitude below the tick approach, and it holds the only rows that answer a
-- real question later: what did the system tell me to do, and what did it do at
-- the broker.
--
-- Retention (prune_intraday) trims alerts on a rolling window. The broker log
-- is kept far longer: it is the audit trail for real money and is tiny.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── Alerts actually sent ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.intraday_alerts (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    symbol      text        NOT NULL,
    kind        text        NOT NULL,   -- BOOK_PARTIAL | EXIT_STOP | ENTRY | GTT_RAISED …
    urgency     text,                   -- CRITICAL | NORMAL | INFO
    headline    text        NOT NULL,
    detail      text,
    ltp         numeric,
    r_multiple  numeric,
    meta        text,
    acknowledged boolean    NOT NULL DEFAULT false
);

COMMENT ON TABLE public.intraday_alerts IS
  'One row per NOTIFIED action — a change in what you should do. Evaluations '
  'that produced no change are not stored: they are ~1,500 per position per '
  'day and carry no information the previous row does not already imply.';

CREATE INDEX IF NOT EXISTS idx_intraday_alerts_ts ON public.intraday_alerts (ts DESC);
CREATE INDEX IF NOT EXISTS idx_intraday_alerts_symbol ON public.intraday_alerts (symbol, ts DESC);


-- ── Broker writes: the audit trail ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.intraday_broker_log (
    id        bigserial PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    symbol    text        NOT NULL,
    channel   text        NOT NULL,     -- GTT | ORDER
    action    text        NOT NULL,     -- PLACED | MODIFIED | CANCELLED | BLOCKED | FAILED
    side      text,                     -- BUY | SELL
    ref_id    text,                     -- broker order/trigger id
    price     numeric,
    quantity  integer,
    detail    text
);

COMMENT ON TABLE public.intraday_broker_log IS
  'Every attempted broker write, INCLUDING the blocked and failed ones. A log '
  'of only successes cannot answer "why did nothing happen at 10:42", which is '
  'the question that actually gets asked. Kept long — it is the money trail '
  'and it is tiny.';

CREATE INDEX IF NOT EXISTS idx_broker_log_ts ON public.intraday_broker_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_broker_log_symbol ON public.intraday_broker_log (symbol, ts DESC);


-- ── Liveness ───────────────────────────────────────────────────────────────
-- Single row, upserted. Without it, "no alerts" is ambiguous: a daemon working
-- correctly on a quiet day and a daemon that died at 09:20 look identical.
CREATE TABLE IF NOT EXISTS public.intraday_heartbeat (
    id          integer PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    summary     text,
    alerts_sent integer DEFAULT 0
);

COMMENT ON TABLE public.intraday_heartbeat IS
  'Proof of life. One row, overwritten — this is state, not history, so it must '
  'never grow.';


-- ── Retention ──────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.prune_intraday(
    alert_days  integer DEFAULT 45,
    broker_days integer DEFAULT 400
)
RETURNS TABLE(alerts_deleted bigint, broker_deleted bigint)
LANGUAGE plpgsql
AS $$
DECLARE a bigint; b bigint;
BEGIN
  DELETE FROM public.intraday_alerts
   WHERE ts < now() - (alert_days || ' days')::interval;
  GET DIAGNOSTICS a = ROW_COUNT;

  -- Broker history outlives alerts by design: an alert is a notification you
  -- either acted on or did not, and it stops mattering within weeks. An order
  -- is a financial record.
  DELETE FROM public.intraday_broker_log
   WHERE ts < now() - (broker_days || ' days')::interval;
  GET DIAGNOSTICS b = ROW_COUNT;

  RETURN QUERY SELECT a, b;
END $$;

COMMENT ON FUNCTION public.prune_intraday IS
  'Rolling retention. Call from the evening pipeline; safe to run repeatedly.';


-- ── Defaults: every gate starts OFF ────────────────────────────────────────
-- Phase 2.0 is monitor-and-notify. Nothing here can touch the broker until it
-- is raised deliberately, and raising the phase alone is still not enough —
-- intraday_gtt_enabled and intraday_orders_enabled are separate switches.
INSERT INTO public.system_config (key, value, description)
VALUES
  ('intraday_autonomy_phase', '2.0',
   '2.0 monitor+notify | 2.5 +GTT stops at the broker | 3.0 +order placement'),
  ('intraday_gtt_enabled', 'false',
   'Phase 2.5 — rest stop-loss GTTs at Zerodha so stops survive this process dying'),
  ('intraday_orders_enabled', 'false',
   'Phase 3 — allow automatic order placement. Requires phase >= 3.0 and DRY_RUN off'),
  ('intraday_auto_exit', 'false',
   'Phase 3 — auto-place SELL orders for exits and partials. Entries stay manual'),
  ('intraday_watch_candidates', 'true',
   'Evaluate TIER_1/TIER_2 candidates intraday for entry-zone touches'),
  ('intraday_eval_interval_s', '15',
   'Seconds between decision evaluations. Ticks update state; this decides'),
  ('intraday_gtt_sync_interval_s', '300',
   'Seconds between reconciling broker GTTs against intended stops'),
  ('intraday_poll_interval_s', '30',
   'REST polling cadence when the websocket is unavailable'),
  ('intraday_rearm_minutes', '45',
   'An unchanged alert repeats only after this long'),
  ('intraday_rearm_critical_minutes', '15',
   'Faster re-arm for CRITICAL alerts — a breached stop should keep nagging'),
  ('intraday_restate_minutes', '5',
   'Same action, changed numbers — minimum gap before restating'),
  ('intraday_max_orders_per_day', '5',      'Phase 3 rail: order count cap'),
  ('intraday_max_order_value', '10000',     'Phase 3 rail: rupee cap per order'),
  ('intraday_max_notional_per_day', '25000','Phase 3 rail: daily notional cap'),
  ('intraday_exit_slip_bps', '30',
   'Marketable-limit slippage for auto exits, in basis points'),
  ('gtt_limit_slippage_pct', '0.5',
   'GTT limit price below trigger. A limit AT the trigger often will not fill '
   'in the fast move a stop exists for'),
  ('gtt_resync_min_pct', '0.4',
   'Minimum stop move worth a broker write, as a percent'),
  ('capital_tolerance_pct', '10',
   'Configured vs broker capital gap treated as agreement'),
  ('capital_block_pct', '25',
   'Gap above which capital reconciliation becomes a blocker')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.system_config WHERE key LIKE 'intraday_%';
  RAISE NOTICE 'intraday config keys present: %', n;
  RAISE NOTICE 'All broker-write gates default to OFF — phase 2.0, monitor and notify only.';
END $$;
