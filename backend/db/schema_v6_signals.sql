-- TradeOS v6 — Signals & Intelligence Schema (Run SECOND)

CREATE TABLE IF NOT EXISTS strategy_config (
    strategy    TEXT PRIMARY KEY,
    params      JSONB NOT NULL,
    enabled     BOOLEAN DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS signal_log (
    id                  BIGSERIAL PRIMARY KEY,
    date                DATE,
    symbol              TEXT,
    company_name        TEXT,
    sector              TEXT,
    strategy            TEXT,
    signal_type         TEXT,
    position_state      TEXT,
    score               NUMERIC,
    in_rule_engine      BOOLEAN,
    in_scanner          BOOLEAN,
    eap_action          TEXT,
    regime              TEXT,
    regime_warning      BOOLEAN DEFAULT FALSE,
    asm_flag            BOOLEAN DEFAULT FALSE,
    fo_ban_flag         BOOLEAN DEFAULT FALSE,
    ai_conviction       TEXT,
    ai_suggested_action TEXT,
    ai_provider         TEXT,
    ai_fallback_used    BOOLEAN DEFAULT FALSE,
    fii_flag            TEXT,
    outcome             TEXT,
    outcome_pnl_pct     NUMERIC,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS regime_history (
    date                DATE PRIMARY KEY,
    regime              TEXT,
    nifty_price         NUMERIC,
    nifty_50dma         NUMERIC,
    nifty_200dma        NUMERIC,
    nifty_weekly_rsi    NUMERIC,
    india_vix           NUMERIC,
    avg_sector_breadth  NUMERIC,
    ctl_enabled         BOOLEAN,
    sbs_enabled         BOOLEAN,
    tpo_enabled         BOOLEAN,
    eap_enabled         BOOLEAN
);

CREATE TABLE IF NOT EXISTS scanner_signals (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE,
    symbol      TEXT,
    pattern     TEXT,
    confidence  NUMERIC,
    details     JSONB,
    in_msl      BOOLEAN,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fii_dii_flow (
    date            DATE PRIMARY KEY,
    fii_gross_buy   NUMERIC,
    fii_gross_sell  NUMERIC,
    fii_net         NUMERIC,
    dii_gross_buy   NUMERIC,
    dii_gross_sell  NUMERIC,
    dii_net         NUMERIC,
    fii_net_5d      NUMERIC,
    fii_net_10d     NUMERIC,
    fii_net_20d     NUMERIC,
    fii_flag        TEXT,
    source          TEXT DEFAULT 'NSE',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS global_cues (
    date                DATE NOT NULL,
    session             TEXT NOT NULL,
    gift_nifty          NUMERIC,
    gift_nifty_chg_pct  NUMERIC,
    gap_signal          TEXT,
    usd_inr             NUMERIC,
    usd_inr_chg_pct     NUMERIC,
    brent_crude         NUMERIC,
    brent_chg_pct       NUMERIC,
    gold_price          NUMERIC,
    us_dow_close        NUMERIC,
    us_nasdaq_close     NUMERIC,
    sector_impacts      JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (date, session)
);

CREATE TABLE IF NOT EXISTS ai_context (
    date                DATE NOT NULL,
    symbol              TEXT NOT NULL,
    conviction          TEXT,
    conviction_reason   TEXT,
    risks               JSONB,
    catalyst            TEXT,
    suggested_action    TEXT,
    strategy_validation TEXT,
    conflicts           TEXT,
    ai_note             TEXT,
    provider            TEXT,
    fallback_used       BOOLEAN DEFAULT FALSE,
    confidence          NUMERIC,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS safety_lists (
    symbol          TEXT NOT NULL,
    list_type       TEXT NOT NULL,
    stage           TEXT,
    reason          TEXT,
    effective_date  DATE,
    source          TEXT DEFAULT 'NSE',
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, list_type)
);

CREATE TABLE IF NOT EXISTS evolution_proposals (
    id                  BIGSERIAL PRIMARY KEY,
    proposed_date       DATE,
    strategy            TEXT,
    param_name          TEXT,
    current_value       TEXT,
    proposed_value      TEXT,
    evidence            TEXT,
    expected_improvement TEXT,
    trades_analyzed     INTEGER,
    status              TEXT DEFAULT 'PENDING',
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    applied_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ml_training_log (
    id              BIGSERIAL PRIMARY KEY,
    trained_at      TIMESTAMPTZ DEFAULT NOW(),
    trades_used     INTEGER,
    features_used   JSONB,
    accuracy        NUMERIC,
    precision_score NUMERIC,
    recall_score    NUMERIC,
    model_version   TEXT,
    notes           TEXT
);

ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS industry          TEXT,
    ADD COLUMN IF NOT EXISTS industry_rank     INT,
    ADD COLUMN IF NOT EXISTS industry_top5     BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS industry_state    TEXT,
    ADD COLUMN IF NOT EXISTS industry_avg_rsi  NUMERIC;

CREATE INDEX IF NOT EXISTS idx_signal_log_date   ON signal_log(date);
CREATE INDEX IF NOT EXISTS idx_signal_log_symbol ON signal_log(symbol);
CREATE INDEX IF NOT EXISTS idx_signal_log_type   ON signal_log(date, signal_type);
CREATE INDEX IF NOT EXISTS idx_scanner_date      ON scanner_signals(date);
CREATE INDEX IF NOT EXISTS idx_fii_date          ON fii_dii_flow(date DESC);
CREATE INDEX IF NOT EXISTS idx_ai_context_date   ON ai_context(date, symbol);
CREATE INDEX IF NOT EXISTS idx_safety_symbol     ON safety_lists(symbol);
