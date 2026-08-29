-- 127_ai_usage_log.sql
--
-- Real token/cost visibility for AI calls — 29-Aug-2026. Audited this
-- session: the three highest-volume AI call sites (ai_decision_engine's
-- evening batches, market_intelligence_engine's evening call, intraday's
-- ai_advisor firing up to ~75x/trading day) reported ZERO cost or token
-- usage anywhere in the code. The only budget mechanism that existed
-- (PROVIDER_COST_INR in post_trade_analysis.py) is a hardcoded per-call
-- guess, not derived from actual resp.usage, and scoped to the smallest,
-- cheapest, least-frequent path. This table is purely additive
-- observability — it changes no trading or AI-call behaviour, only makes
-- what already happens measurable.
--
-- Best-effort by design: ai/usage_tracker.py::log_usage() wraps every
-- write in try/except so a logging failure can never break the AI call
-- it is observing.

CREATE TABLE IF NOT EXISTS public.ai_usage_log (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
    call_site         TEXT NOT NULL,   -- 'ai_decision_engine', 'market_intelligence_engine',
                                        -- 'intraday_ai_advisor', 'post_trade_analysis', 'llm_synthesizer', ...
    framework         TEXT,            -- 'SWING' / 'INTRADAY' / null (book-level or n/a)
    provider          TEXT,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    finish_reason     TEXT             -- 'stop' / 'length' (truncated) / provider-specific / null
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_log_ts ON public.ai_usage_log (ts);
CREATE INDEX IF NOT EXISTS idx_ai_usage_log_call_site ON public.ai_usage_log (call_site);
