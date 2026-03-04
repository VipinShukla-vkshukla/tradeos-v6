-- TradeOS v6 — Row Level Security (Run LAST)
-- Restricts all tables to authenticated users only

ALTER TABLE stock_data_daily    ENABLE ROW LEVEL SECURITY;
ALTER TABLE master_shortlist    ENABLE ROW LEVEL SECURITY;
ALTER TABLE open_positions      ENABLE ROW LEVEL SECURITY;
ALTER TABLE closed_positions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE sector_strength     ENABLE ROW LEVEL SECURITY;
ALTER TABLE market_regime       ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_calendar      ENABLE ROW LEVEL SECURITY;
ALTER TABLE lessons             ENABLE ROW LEVEL SECURITY;
ALTER TABLE msl_history         ENABLE ROW LEVEL SECURITY;
ALTER TABLE nse_holidays        ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_config       ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_config     ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_log          ENABLE ROW LEVEL SECURITY;
ALTER TABLE regime_history      ENABLE ROW LEVEL SECURITY;
ALTER TABLE industry_strength   ENABLE ROW LEVEL SECURITY;

-- Service role bypasses RLS (pipeline uses service key)
-- anon role gets read-only on non-sensitive tables
CREATE POLICY "anon_read_stock_data"    ON stock_data_daily    FOR SELECT USING (true);
CREATE POLICY "anon_read_msl"           ON master_shortlist    FOR SELECT USING (true);
CREATE POLICY "anon_read_open_pos"      ON open_positions      FOR SELECT USING (true);
CREATE POLICY "anon_read_sectors"       ON sector_strength     FOR SELECT USING (true);
CREATE POLICY "anon_read_regime"        ON market_regime       FOR SELECT USING (true);
CREATE POLICY "anon_read_events"        ON event_calendar      FOR SELECT USING (true);
CREATE POLICY "anon_read_signals"       ON signal_log          FOR SELECT USING (true);
CREATE POLICY "anon_read_config"        ON system_config       FOR SELECT USING (true);
CREATE POLICY "anon_read_strategy_cfg"  ON strategy_config     FOR SELECT USING (true);
CREATE POLICY "anon_read_industry"      ON industry_strength   FOR SELECT USING (true);
