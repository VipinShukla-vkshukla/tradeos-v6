-- ============================================================
-- TradeOS v6 — screen_stocks_migration.sql
-- Run in Supabase SQL Editor BEFORE deploying screen_stocks.py
-- ============================================================

-- ── 1. msl_computed — add screener columns (if not already from compute_msl) ──

ALTER TABLE msl_computed
    ADD COLUMN IF NOT EXISTS priority_rank     INTEGER,
    ADD COLUMN IF NOT EXISTS engines_list      TEXT,
    ADD COLUMN IF NOT EXISTS convergence_pts   NUMERIC,
    ADD COLUMN IF NOT EXISTS eap_adjustment    NUMERIC,
    ADD COLUMN IF NOT EXISTS screener_source   TEXT;

-- ── 2. master_shortlist — add screener columns ───────────────────────────────

ALTER TABLE master_shortlist
    ADD COLUMN IF NOT EXISTS force_include     BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS engines_list      TEXT,
    ADD COLUMN IF NOT EXISTS convergence_pts   NUMERIC,
    ADD COLUMN IF NOT EXISTS eap_adjustment    NUMERIC;

COMMENT ON COLUMN master_shortlist.force_include IS
  'If TRUE, this symbol is always included regardless of screener output. '
  'Used for manual overrides via the Google Sheet MSL tab.';

-- ── 3. system_config — screener control keys ─────────────────────────────────

INSERT INTO system_config (key, value, description) VALUES
('screener_mode',            'shadow',  'screen_stocks.py write mode: shadow | hybrid | full'),
('screener_max_symbols',     '30',      'Maximum symbols to select per daily run'),
('screener_max_per_sector',  '5',       'Sector diversification cap per screener run')
ON CONFLICT (key) DO NOTHING;

-- ── 4. Reload schema cache ────────────────────────────────────────────────────
NOTIFY pgrst, 'reload schema';


-- ── 5. Transition commands ────────────────────────────────────────────────────

-- After 2+ weeks of shadow validation (compare msl_computed vs Sheet MSL):
-- UPDATE system_config SET value='hybrid' WHERE key='screener_mode';

-- After 4+ weeks of hybrid (screener-driven signals performing well):
-- UPDATE system_config SET value='full' WHERE key='screener_mode';

-- Manual symbol override (force a stock into MSL regardless of screener):
-- UPDATE master_shortlist SET force_include=TRUE WHERE symbol='POWERINDIA' AND date=CURRENT_DATE;

-- Revert to shadow at any time:
-- UPDATE system_config SET value='shadow' WHERE key='screener_mode';


-- ── 6. Useful validation query — compare screener vs Sheet MSL ───────────────

SELECT
    COALESCE(c.symbol, m.symbol)      AS symbol,
    c.priority_rank                    AS screener_rank,
    c.composite_score                  AS screener_score,
    c.engines_list                     AS engines,
    m.final_score                      AS sheet_score,
    m.base_rank                        AS sheet_rank,
    m.strategy_source                  AS sheet_strategy,
    CASE
        WHEN c.symbol IS NULL    THEN '🔴 In Sheet, NOT in screener'
        WHEN m.symbol IS NULL    THEN '🟢 In screener, NOT in Sheet'
        ELSE '✅ In both'
    END                                AS status
FROM msl_computed c
FULL OUTER JOIN master_shortlist m
    ON c.symbol = m.symbol AND c.date = m.date
WHERE COALESCE(c.date, m.date) = CURRENT_DATE
ORDER BY COALESCE(c.priority_rank, 99);


-- ── 7. Engine hit frequency analysis (run after 1+ weeks of shadow mode) ─────

SELECT
    REGEXP_SPLIT_TO_TABLE(engines_list, ',') AS engine,
    COUNT(*)                                   AS symbols_hit,
    ROUND(AVG(composite_score), 1)             AS avg_score,
    COUNT(DISTINCT DATE(computed_at))          AS days_appeared
FROM msl_computed
WHERE computed_at >= NOW() - INTERVAL '14 days'
GROUP BY 1
ORDER BY 2 DESC;
