-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 033: decision-input integrity (Stage 5)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE GAP BETWEEN WHAT A DECISION READS AND WHEN IT WAS TRUE
-- ----------------------------------------------------------
-- refresh_contexts() assembles bars, day range, volume and the benchmark price
-- on the 300-second slow timer, because the historical endpoint is rate-limited
-- far more tightly than quotes. The decision loop runs every 15 seconds. So for
-- up to five minutes a breakout is measured against a range that stopped
-- earlier: a 10:47 signal judged on a 10:45 high.
--
-- MODE_QUOTE carries day range, cumulative volume and the exchange's own
-- average traded price on the SAME websocket that already carries last price,
-- at no additional API cost. MODE_FULL is still not requested — it adds market
-- depth, which nothing in this system reads, at a large bandwidth cost.
--
-- THE STALENESS GUARD IS THE DELIVERABLE, NOT THE UPGRADE
-- -------------------------------------------------------
-- Quote mode narrows the window. It does not close the failure that has no
-- upper bound at all: a dead socket with a live cache. When a refresh fails —
-- broker unavailable, rate limit, socket dead — the previous contexts stay in
-- memory and keep being read, and nothing about that reads as broken. The
-- engines run, prices tick, verdicts look ordinary, and every reference level
-- is from whenever the failure began. "Live" becomes confident garbage.
--
-- SymbolContext now carries `as_of`, every consumer can ask its age, and
-- UNKNOWN AGE COUNTS AS STALE. The alternative reads "we do not know when this
-- was true, so proceed", which is the reasoning that lets a dead feed trade.
--
-- Entries are gated on it. EXITS ARE NOT: a stale day range is a bad reason to
-- open a position and never a reason to stop protecting one that is open.
--
-- PARITY BEFORE TRUST
-- -------------------
-- intraday_quote_mode defaults to FALSE. The specification requires both
-- sources logged for one session and the divergence reported before the live
-- fields are believed — see tools/quote_parity. A feed that disagrees with the
-- historical endpoint about today's high is not an upgrade, it is a new bug
-- with better latency.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_quote_mode', 'false',
   'Subscribe the websocket in QUOTE mode so day range, session volume and the '
   'exchange average price arrive live instead of on the 300s historical '
   'refresh. False keeps LTP mode and the prior behaviour exactly. Enable only '
   'after tools/quote_parity shows the two sources agree for a session.',
   'Intraday', 'intraday/price_feed.py', 'bool', 'false', 'CRITICAL'),

  ('intraday_context_max_age_s', '420',
   'How old a symbol context may be and still support a NEW ENTRY. Contexts '
   'rebuild every 300s, so 420 tolerates one late refresh and refuses a refresh '
   'that did not happen. Exits ignore this — a stale range must never stop a '
   'position being protected. Unknown age counts as stale.',
   'Intraday', 'intraday/engine.py', 'float', '420', 'CRITICAL'),

  ('intraday_quote_parity_log', 'false',
   'Record both the live quote and the historical value for the same field, '
   'each slow timer, into intraday_quote_parity. Costs one row per symbol per '
   '300s and is meant to run for one session, not permanently.',
   'Intraday', 'tools/quote_parity.py', 'bool', 'false', 'TUNING')
ON CONFLICT (key) DO NOTHING;


-- ── Where the parity evidence lands ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.intraday_quote_parity (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    symbol      text        NOT NULL,
    field       text        NOT NULL,     -- day_high | day_low | vwap | volume
    live_value  numeric,                  -- from the tick stream
    fetched_value numeric,                -- from historical_data / quote REST
    diff_pct    numeric
);

CREATE INDEX IF NOT EXISTS idx_quote_parity_ts ON public.intraday_quote_parity (ts DESC);

COMMENT ON TABLE public.intraday_quote_parity IS
  'One session of evidence that the websocket and the historical endpoint agree '
  'about the same field before the faster one is trusted. Deliberately small and '
  'deliberately temporary — intraday_quote_parity_log defaults off, and this '
  'table is expected to hold one session, not a history.';


DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Stage 5 wired. Nothing changed behaviour yet:';
  RAISE NOTICE '  intraday_quote_mode = false  (LTP mode, exactly as before)';
  RAISE NOTICE '';
  RAISE NOTICE 'The staleness guard IS active, and refuses new entries on a context';
  RAISE NOTICE 'older than 420s or of unknown age. Exits are deliberately not gated.';
  RAISE NOTICE '';
  RAISE NOTICE 'To earn quote mode:';
  RAISE NOTICE '  1. UPDATE system_config SET value=''true'' WHERE key=''intraday_quote_parity_log'';';
  RAISE NOTICE '  2. run one session';
  RAISE NOTICE '  3. cd backend && python -m tools.quote_parity';
END $$;
