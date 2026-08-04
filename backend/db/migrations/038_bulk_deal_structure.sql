-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 038: keep the numbers the bulk-deal feed already has
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE SOURCE WAS NEVER MISSING. THE STRUCTURE WAS.
--
-- Every planning document treats block and bulk deal ingestion as Phase 4's one
-- new external dependency — PRODUCTION_DECISION calls it "one new public
-- dataset", and the readiness review's M3 flags it as having no source
-- specified and warns the accumulation engine may have to degrade without it.
--
-- It has been ingested since roughly March 2026. `fetch_nse_bulk_deals()` in
-- swing/ingestion/ingest_market_news.py calls NSE's
-- snapshot-capital-market-largedeal endpoint, reads BOTH BULK_DEALS_DATA and
-- BLOCK_DEALS_DATA, and market_news holds 1,585 rows of it. No new dependency,
-- no new endpoint, no new failure mode.
--
-- What it does with the data is the problem. The fetcher parses symbol, client
-- name, buy/sell and quantity and weighted price — and then formats all four
-- into one sentence:
--
--     "ADANI INFRA (INDIA) LIMITED BUY ADANIGREEN: 17,000,000 shares @ Rs1400"
--
-- That is exactly right for the alert it was built for, and useless for an
-- engine. An accumulation-confirmed engine has to ask "net institutional buying
-- in this name over twenty sessions, against its average traded value" — which
-- needs side, quantity and value as NUMBERS. Recovering them by regex from a
-- headline is not ingestion, it is archaeology.
--
-- This is the same shape as three other findings in this project: win
-- probability computed and discarded, the plan outcome columns written as NULL,
-- the roll-off function written and never called. The work was done and the
-- result was dropped on the floor one step before it became useful.
--
-- ALSO FIXED, IN THE FETCHER: the [:15] slice per deal type. On a busy session
-- NSE reports well over thirty large deals and the tail was silently dropped —
-- and the tail is where mid-cap accumulation lives, which is precisely the
-- population the engine cares about.
--
-- The existing market_news write is UNCHANGED. Alerts keep reading exactly what
-- they read today; this table is written alongside it.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.bulk_deals (
    id          bigserial PRIMARY KEY,
    deal_date   date        NOT NULL,
    symbol      text        NOT NULL,
    deal_type   text        NOT NULL,          -- BULK | BLOCK
    client_name text        NOT NULL,
    side        text        NOT NULL,          -- BUY | SELL
    quantity    numeric     NOT NULL,
    price       numeric,
    value_cr    numeric,                       -- quantity * price / 1e7, for sizing
    ingested_at timestamptz NOT NULL DEFAULT now(),
    -- One deal is one (date, symbol, client, side, qty). Re-running the evening
    -- pipeline must not double-count institutional buying — the whole engine
    -- rests on net quantity, so a duplicate is not a cosmetic problem.
    UNIQUE (deal_date, symbol, client_name, side, quantity)
);

CREATE INDEX IF NOT EXISTS idx_bulk_deals_symbol_date
    ON public.bulk_deals (symbol, deal_date DESC);
CREATE INDEX IF NOT EXISTS idx_bulk_deals_date
    ON public.bulk_deals (deal_date DESC);

COMMENT ON TABLE public.bulk_deals IS
  'Structured NSE bulk and block deals. The same feed market_news has carried '
  'as prose since March 2026, with the numbers kept. Written by '
  'ingest_market_news.fetch_nse_bulk_deals alongside the existing headline row, '
  'which is unchanged.';

COMMENT ON COLUMN public.bulk_deals.value_cr IS
  'Deal value in crore. The engine sorts on persistence of net buying relative '
  'to a name''s ordinary traded value, so absolute quantity is not comparable '
  'across symbols and this is what makes them so.';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('bulk_deal_capture_enabled', 'true',
   'Write structured bulk/block deals to bulk_deals in addition to the existing '
   'market_news headline. Off reproduces prior behaviour: the numbers are '
   'formatted into a sentence and discarded.',
   'Flows', 'swing/ingestion/ingest_market_news.py', 'bool', 'false', 'TUNING')
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM public.market_news WHERE source = 'NSE_BULK_DEAL';
  RAISE NOTICE '';
  RAISE NOTICE 'bulk_deals ready. % existing headline rows stay exactly as they are.', n;
  RAISE NOTICE 'Structured capture starts with tonight''s pipeline run.';
  RAISE NOTICE '';
  RAISE NOTICE 'The historical 1,585 rows are NOT backfilled: entity, side and';
  RAISE NOTICE 'quantity are recoverable from those headlines by regex, but a prior';
  RAISE NOTICE 'built on re-parsed prose is not evidence. The engine waits for';
  RAISE NOTICE 'real rows.';
END $$;
