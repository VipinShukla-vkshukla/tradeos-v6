-- 024_risk_coherence.sql
--
-- Make every risk number tell one story against a Rs 20,000 account, and make
-- the per-framework caps enforceable.
--
-- WHY
-- ---
-- The caps were carried over from a larger account and never rebased. A
-- per-order cap of Rs 25,000 on a Rs 20,000 account cannot bind, so it protects
-- nothing — and the entire reason a hard rupee ceiling exists is to catch a
-- sizing bug, because every sizing INPUT (capital, risk percent, ATR) has been
-- wrong at some point in this project.
--
-- Separately, intraday_broker_log had no framework column, so today's spend
-- could not be attributed. The per-framework daily caps were therefore
-- unenforceable: swing orders consumed the intraday budget and vice versa.

-- ── 1. Attribute broker activity to a framework ────────────────────────────
ALTER TABLE intraday_broker_log
    ADD COLUMN IF NOT EXISTS framework TEXT;

COMMENT ON COLUMN intraday_broker_log.framework IS
    'SWING or INTRADAY. Read by order_manager._today_totals() to scope the '
    'daily order-count and notional caps. NULL on rows written before this '
    'migration; those count against whichever framework is asking, which can '
    'only make preflight stricter.';

CREATE INDEX IF NOT EXISTS idx_broker_log_fw_ts
    ON intraday_broker_log (framework, ts DESC)
    WHERE channel = 'ORDER';

-- ── 2. Rebase the caps on the capital that actually funds them ─────────────
-- Sizing tops out at 20% of capital = Rs 4,000 per swing position. The cap sits
-- at 1.5x that: high enough never to block a legitimate trade, low enough to
-- catch a sizing bug long before it reaches the account balance.
UPDATE system_config SET value = '6000',  updated_at = NOW()
    WHERE key = 'swing_max_order_value';

-- 4 orders x Rs 4,000 is a fully-deployed swing book. Recycling the whole
-- account more than once in a day is not swing trading.
UPDATE system_config SET value = '20000', updated_at = NOW()
    WHERE key = 'swing_max_notional_per_day';

-- Intraday sizes at 25% of capital = Rs 5,000; Rs 6,000 is the bug-catcher
-- above it. The old Rs 10,000 was half the account in one intraday trade.
UPDATE system_config SET value = '6000',  updated_at = NOW()
    WHERE key = 'intraday_max_order_value';

UPDATE system_config SET value = '20000', updated_at = NOW()
    WHERE key = 'intraday_max_notional_per_day';

-- ── 3. The fraction intraday sizing was missing entirely ───────────────────
-- Intraday budget was TOTAL_CAPITAL x market_multiplier, i.e. 100% of the
-- account in one setup on a risk-on day. Swing has had max_position_pct since
-- the beginning; intraday never got the equivalent.
INSERT INTO system_config (key, value, description, category, risk_level)
VALUES ('intraday_max_position_pct', '25',
        'Largest share of TOTAL_CAPITAL one intraday position may take, before '
        'the market-state multiplier. Read by intraday/engine.py when sizing.',
        'RISK', 'CRITICAL')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- ── 4. Paper must simulate the account you actually have ───────────────────
-- Rs 100,000 of paper capital against a Rs 20,000 account means paper takes
-- positions the real account could never fund, so its results do not transfer
-- — which defeats the only purpose paper trading has.
UPDATE system_config SET value = '20000', updated_at = NOW()
    WHERE key = 'paper_starting_capital';

-- 4 positions x Rs 5,000 = the full account. 5 was inherited from the larger
-- paper capital and would deploy more than the account holds.
UPDATE system_config SET value = '4', updated_at = NOW()
    WHERE key = 'paper_max_open_positions';
