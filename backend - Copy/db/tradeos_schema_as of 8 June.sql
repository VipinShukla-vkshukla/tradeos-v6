


SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pg_trgm" WITH SCHEMA "public";






CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";






CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";






CREATE OR REPLACE FUNCTION "public"."get_public_tables"() RETURNS TABLE("table_name" "text")
    LANGUAGE "sql" STABLE SECURITY DEFINER
    AS $$
    select tablename::text as table_name
    from   pg_tables
    where  schemaname = 'public'
    order  by tablename;
$$;


ALTER FUNCTION "public"."get_public_tables"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_symbol_history_summary"("p_symbols" "text"[], "p_cutoff" "date", "p_today" "date") RETURNS TABLE("symbol" "text", "row_count" bigint, "latest_date" "date", "first_date" "date")
    LANGUAGE "sql" STABLE
    AS $$
    SELECT
        symbol,
        COUNT(*)::BIGINT   AS row_count,
        MAX(date)::DATE    AS latest_date,
        MIN(date)::DATE    AS first_date
    FROM price_history_yf
    WHERE symbol  = ANY(p_symbols)
      AND date   >= p_cutoff
      AND date   <= p_today
    GROUP BY symbol;
$$;


ALTER FUNCTION "public"."get_symbol_history_summary"("p_symbols" "text"[], "p_cutoff" "date", "p_today" "date") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_table_columns"("p_table_name" "text") RETURNS TABLE("column_name" "text", "data_type" "text", "is_nullable" "text", "ordinal_position" integer)
    LANGUAGE "sql" SECURITY DEFINER
    AS $$
  SELECT
    column_name::text,
    data_type::text,
    is_nullable::text,
    ordinal_position
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = p_table_name
  ORDER BY ordinal_position;
$$;


ALTER FUNCTION "public"."get_table_columns"("p_table_name" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."rls_auto_enable"() RETURNS "event_trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog'
    AS $$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$$;


ALTER FUNCTION "public"."rls_auto_enable"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."safe_jsonb"("txt" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" IMMUTABLE
    AS $$
BEGIN
  RETURN txt::jsonb;
EXCEPTION WHEN others THEN
  RETURN NULL;
END;
$$;


ALTER FUNCTION "public"."safe_jsonb"("txt" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_msl_history_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."set_msl_history_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
begin new.updated_at = now(); return new; end; $$;


ALTER FUNCTION "public"."set_updated_at"() OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."ai_context" (
    "date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "conviction" "text",
    "conviction_reason" "text",
    "risks" "jsonb",
    "catalyst" "text",
    "suggested_action" "text",
    "strategy_validation" "text",
    "conflicts" "text",
    "ai_note" "text",
    "provider" "text",
    "fallback_used" boolean DEFAULT false,
    "confidence" numeric,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."ai_context" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."ai_model_performance" (
    "id" bigint NOT NULL,
    "date" "date" DEFAULT CURRENT_DATE NOT NULL,
    "provider" "text" NOT NULL,
    "model" "text",
    "calls_today" integer DEFAULT 0,
    "cost_today" numeric DEFAULT 0,
    "accuracy" numeric,
    "avg_confidence" numeric,
    "fallback_used" boolean DEFAULT false,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."ai_model_performance" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."ai_model_performance_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."ai_model_performance_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."ai_model_performance_id_seq" OWNED BY "public"."ai_model_performance"."id";



CREATE TABLE IF NOT EXISTS "public"."brain_analysis_log" (
    "id" bigint NOT NULL,
    "run_id" "text" NOT NULL,
    "run_date" "date" NOT NULL,
    "period_days" integer NOT NULL,
    "run_mode" "text" DEFAULT 'full'::"text",
    "signals_analyzed" integer DEFAULT 0,
    "signals_with_outcomes" integer DEFAULT 0,
    "coverage_pct" numeric,
    "tables_loaded" "jsonb",
    "scripts_analyzed" "jsonb",
    "quant_findings" "jsonb",
    "llm_insights" "text",
    "proposals_generated" integer DEFAULT 0,
    "proposals_auto_applied" integer DEFAULT 0,
    "script_patches_applied" integer DEFAULT 0,
    "execution_time_sec" numeric,
    "errors" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."brain_analysis_log" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."brain_analysis_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."brain_analysis_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."brain_analysis_log_id_seq" OWNED BY "public"."brain_analysis_log"."id";



CREATE TABLE IF NOT EXISTS "public"."brain_proposals" (
    "id" bigint NOT NULL,
    "analysis_run_id" "text" NOT NULL,
    "proposal_type" "text" NOT NULL,
    "target_key" "text",
    "current_value" "text",
    "proposed_value" "text",
    "rationale" "text" NOT NULL,
    "evidence" "jsonb",
    "backtest_result" "jsonb",
    "confidence" numeric,
    "status" "text" DEFAULT 'PENDING'::"text" NOT NULL,
    "source" "text" DEFAULT 'hybrid'::"text" NOT NULL,
    "priority" integer DEFAULT 5,
    "high_impact" boolean DEFAULT false,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "expires_at" timestamp with time zone DEFAULT ("now"() + '30 days'::interval),
    "reviewed_at" timestamp with time zone,
    "reviewed_by" "text",
    "applied_at" timestamp with time zone,
    "rollback_value" "text",
    "script_diff" "text",
    "script_backup_path" "text",
    "test_run_id" "text",
    "hardcoded_values" "text",
    CONSTRAINT "brain_proposals_confidence_check" CHECK ((("confidence" >= (0)::numeric) AND ("confidence" <= (1)::numeric)))
);


ALTER TABLE "public"."brain_proposals" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."brain_proposals_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."brain_proposals_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."brain_proposals_id_seq" OWNED BY "public"."brain_proposals"."id";



CREATE TABLE IF NOT EXISTS "public"."brain_script_registry" (
    "id" bigint NOT NULL,
    "script_path" "text" NOT NULL,
    "module_name" "text",
    "purpose" "text",
    "tables_read" "text"[],
    "tables_written" "text"[],
    "config_keys_used" "text"[],
    "hardcoded_values" "jsonb",
    "brain_coverage" "text" DEFAULT 'PARTIAL'::"text",
    "last_scanned" timestamp with time zone,
    "last_modified" timestamp with time zone,
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."brain_script_registry" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."brain_script_registry_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."brain_script_registry_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."brain_script_registry_id_seq" OWNED BY "public"."brain_script_registry"."id";



CREATE TABLE IF NOT EXISTS "public"."chartink_raw_data" (
    "id" bigint NOT NULL,
    "date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "sector" "text",
    "industry" "text",
    "market_cap" numeric,
    "market_cap_cat" "text",
    "daily_open" numeric,
    "daily_high" numeric,
    "daily_low" numeric,
    "daily_close" numeric,
    "week52_high" numeric,
    "week52_low" numeric,
    "high_30d" numeric,
    "sma_10" numeric,
    "sma_20" numeric,
    "sma_50" numeric,
    "sma_200" numeric,
    "ema_10" numeric,
    "ema_20" numeric,
    "ema_50" numeric,
    "rsi_daily" numeric,
    "rsi_weekly" numeric,
    "rsi_monthly" numeric,
    "adx_14" numeric,
    "adx_plus_di" numeric,
    "adx_minus_di" numeric,
    "volume" bigint,
    "avg_vol_20" numeric,
    "avg_vol_50" numeric,
    "vwap_daily" numeric,
    "vwap_20d" numeric,
    "vwap_50d" numeric,
    "pct_change" numeric,
    "atr_14" numeric,
    "atr_pct" numeric,
    "ha_high" numeric,
    "ha_low" numeric,
    "ha_close" numeric,
    "supertrend" numeric,
    "macd_line" numeric,
    "macd_signal" numeric,
    "macd_histogram" numeric,
    "parabolic_sar" numeric,
    "upper_bb" numeric,
    "lower_bb" numeric,
    "stochastic" numeric,
    "ttm_net_profit" numeric,
    "net_profit_yr" numeric,
    "eps" numeric,
    "qtr_net_profit" numeric,
    "qtr_var_profit" numeric,
    "ingested_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."chartink_raw_data" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."chartink_raw_data_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."chartink_raw_data_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."chartink_raw_data_id_seq" OWNED BY "public"."chartink_raw_data"."id";



CREATE TABLE IF NOT EXISTS "public"."closed_positions" (
    "id" bigint NOT NULL,
    "symbol" "text",
    "company_name" "text",
    "sector" "text",
    "strategy" "text",
    "entry_date" "date",
    "entry_price" numeric,
    "proposed_qty" numeric,
    "actual_qty" numeric,
    "invested_value" numeric,
    "exit_date" "date",
    "exit_price" numeric,
    "exit_value" numeric,
    "realized_pnl" numeric,
    "pnl_pct" numeric,
    "high_water_mark" numeric,
    "max_favorable_excursion" numeric,
    "lifecycle_at_entry" "text",
    "entry_timing_type" "text",
    "reentry_mode" "text",
    "expected_r_at_entry" numeric,
    "exit_reason" "text",
    "signal_date" "date"
);


ALTER TABLE "public"."closed_positions" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."closed_positions_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."closed_positions_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."closed_positions_id_seq" OWNED BY "public"."closed_positions"."id";



CREATE TABLE IF NOT EXISTS "public"."config_change_log" (
    "id" bigint NOT NULL,
    "key" "text" NOT NULL,
    "old_value" "text",
    "new_value" "text",
    "changed_by" "text" DEFAULT 'brain_engine'::"text" NOT NULL,
    "proposal_id" bigint,
    "reason" "text",
    "changed_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "rolled_back_at" timestamp with time zone,
    "rolled_back_by" "text"
);


ALTER TABLE "public"."config_change_log" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."config_change_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."config_change_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."config_change_log_id_seq" OWNED BY "public"."config_change_log"."id";



CREATE TABLE IF NOT EXISTS "public"."custom_views" (
    "id" "text" NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "base_table" "text" NOT NULL,
    "columns" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "joins" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "filters" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "sorts" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "view_type" "text" DEFAULT 'table'::"text" NOT NULL,
    "chart_config" "jsonb",
    "tab_id" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."custom_views" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."data_anomalies" (
    "id" bigint NOT NULL,
    "date" "date",
    "check_name" "text",
    "severity" "text",
    "value" "text",
    "message" "text",
    "affected" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."data_anomalies" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."data_anomalies_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."data_anomalies_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."data_anomalies_id_seq" OWNED BY "public"."data_anomalies"."id";



CREATE TABLE IF NOT EXISTS "public"."event_calendar" (
    "id" bigint NOT NULL,
    "event_category" "text",
    "event_name" "text",
    "event_type" "text",
    "start_date" "date",
    "end_date" "date",
    "affected_sectors" "text",
    "event_bias" "text",
    "event_intensity" "text",
    "strategy_impact" "text",
    "notes" "text",
    "event_status" "text",
    "is_active" boolean,
    "priority" integer,
    "is_risk_on_accelerator" "text"
);


ALTER TABLE "public"."event_calendar" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."event_calendar_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."event_calendar_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."event_calendar_id_seq" OWNED BY "public"."event_calendar"."id";



CREATE TABLE IF NOT EXISTS "public"."evolution_proposals" (
    "id" bigint NOT NULL,
    "proposed_date" "date",
    "strategy" "text",
    "param_name" "text",
    "current_value" "text",
    "proposed_value" "text",
    "evidence" "text",
    "expected_improvement" "text",
    "trades_analyzed" integer,
    "status" "text" DEFAULT 'PENDING'::"text",
    "approved_by" "text",
    "approved_at" timestamp with time zone,
    "applied_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "week_of" "text",
    "confidence" numeric,
    "impact_measured_at" timestamp with time zone,
    "performance_delta" numeric,
    "notes" "text"
);


ALTER TABLE "public"."evolution_proposals" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."evolution_proposals_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."evolution_proposals_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."evolution_proposals_id_seq" OWNED BY "public"."evolution_proposals"."id";



CREATE TABLE IF NOT EXISTS "public"."fii_dii_flow" (
    "date" "date" NOT NULL,
    "fii_gross_buy_cr" numeric,
    "fii_gross_sell_cr" numeric,
    "fii_net_cr" numeric,
    "dii_gross_buy_cr" numeric,
    "dii_gross_sell_cr" numeric,
    "dii_net_cr" numeric,
    "source" "text" DEFAULT 'NSE'::"text",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "fii_gross_buy" numeric,
    "fii_gross_sell" numeric,
    "fii_net" numeric,
    "dii_gross_buy" numeric,
    "dii_gross_sell" numeric,
    "dii_net" numeric,
    "fii_net_5d" numeric,
    "fii_net_10d" numeric,
    "fii_net_20d" numeric,
    "fii_flag" "text",
    "dii_net_5d" numeric,
    "dii_net_20d" numeric,
    "dii_flag" "text"
);


ALTER TABLE "public"."fii_dii_flow" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."global_cues" (
    "date" "date" NOT NULL,
    "session" "text" NOT NULL,
    "gift_nifty" numeric,
    "gift_nifty_chg_pct" numeric,
    "gap_signal" "text",
    "usd_inr" numeric,
    "usd_inr_chg_pct" numeric,
    "brent_crude" numeric,
    "brent_chg_pct" numeric,
    "gold_price" numeric,
    "us_dow_close" numeric,
    "us_nasdaq_close" numeric,
    "sector_impacts" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "us_dow_chg_pct" numeric,
    "us_nasdaq_chg_pct" numeric,
    "sp500_close" numeric,
    "sp500_chg_pct" numeric,
    "us_10yr_yield" numeric,
    "us_10yr_chg_bps" numeric,
    "silver_price" numeric,
    "silver_chg_pct" numeric
);


ALTER TABLE "public"."global_cues" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."industry_strength" (
    "id" bigint NOT NULL,
    "date" "date" NOT NULL,
    "industry" "text" NOT NULL,
    "stock_count" integer,
    "avg_rsi_daily" numeric,
    "avg_rsi_weekly" numeric,
    "avg_rsi_monthly" numeric,
    "avg_ret_6m" numeric,
    "breadth_sma50" numeric,
    "rank" integer,
    "top5_flag" boolean,
    "industry_state" "text",
    "synced_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."industry_strength" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."industry_strength_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."industry_strength_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."industry_strength_id_seq" OWNED BY "public"."industry_strength"."id";



CREATE TABLE IF NOT EXISTS "public"."lessons" (
    "id" bigint NOT NULL,
    "date" "date",
    "scenario_type" "text",
    "trigger_event" "text",
    "linked_event_type" "text",
    "impacted_sector" "text",
    "scenario_context" "text",
    "what_expected" "text",
    "what_happened" "text",
    "what_failed" "text",
    "root_cause" "text",
    "corrective_rule" "text",
    "source" "text" DEFAULT 'MANUAL'::"text",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "is_active" boolean DEFAULT true,
    "times_applied" integer DEFAULT 0,
    "times_worked" integer DEFAULT 0,
    "confidence" numeric DEFAULT 1.0,
    "linked_symbols" "text"[] DEFAULT '{}'::"text"[],
    "observation" "text"
);


ALTER TABLE "public"."lessons" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."lessons_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."lessons_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."lessons_id_seq" OWNED BY "public"."lessons"."id";



CREATE TABLE IF NOT EXISTS "public"."macro_indicators" (
    "id" bigint NOT NULL,
    "indicator_date" "date" NOT NULL,
    "indicator_name" "text" NOT NULL,
    "indicator_value" numeric,
    "previous_value" numeric,
    "change_bps" numeric,
    "source" "text",
    "release_date" "date",
    "ingested_at" timestamp with time zone DEFAULT "now"(),
    "is_cached" boolean DEFAULT false
);


ALTER TABLE "public"."macro_indicators" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."macro_indicators_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."macro_indicators_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."macro_indicators_id_seq" OWNED BY "public"."macro_indicators"."id";



CREATE TABLE IF NOT EXISTS "public"."market_news" (
    "id" bigint NOT NULL,
    "news_date" "date" DEFAULT CURRENT_DATE NOT NULL,
    "headline" "text" NOT NULL,
    "source" "text",
    "category" "text",
    "impact_type" "text",
    "parsed_sectors" "text"[],
    "parsed_symbols" "text"[],
    "magnitude" "text",
    "raw_url" "text",
    "ingested_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."market_news" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."market_news_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."market_news_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."market_news_id_seq" OWNED BY "public"."market_news"."id";



CREATE TABLE IF NOT EXISTS "public"."market_regime" (
    "date" "date" NOT NULL,
    "regime" "text",
    "nifty_price" numeric,
    "nifty_50dma" numeric,
    "nifty_200dma" numeric,
    "nifty_weekly_rsi" numeric,
    "india_vix" numeric,
    "gift_nifty" numeric,
    "avg_sector_breadth" numeric,
    "raw_data" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "predicted_regime" "text",
    "regime_confidence" numeric,
    "regime_predicted_at" timestamp with time zone,
    "regime_score" double precision,
    "nifty_1d_chg_pct" double precision,
    "nifty_5d_chg_pct" double precision,
    "nifty_20d_chg_pct" double precision,
    "advance_decline_ratio" double precision,
    "above_200dma_pct" double precision,
    "computed_regime" "text",
    "regime_score_computed" numeric,
    "regime_score_breakdown" "jsonb",
    "regime_computed_at" timestamp with time zone,
    "banknifty_price" numeric,
    "banknifty_weekly_rsi" numeric,
    "vix_5d_delta" numeric,
    "gift_premium_pct" numeric,
    "sp500_5d_ret" numeric,
    "usdinr_5d_chg" numeric,
    "midcap_breadth" numeric,
    "smallcap_breadth" numeric,
    "nifty_pcr" numeric,
    "macro_boost" numeric
);


ALTER TABLE "public"."market_regime" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."master_shortlist" (
    "date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "company_name" "text",
    "sector" "text",
    "strategy_source" "text",
    "current_price" numeric,
    "final_score" numeric,
    "days_in_list" integer,
    "rank_vel_3d" numeric,
    "score_vel_5d" numeric,
    "momentum_state" "text",
    "momentum_phase" "text",
    "velocity_state" "text",
    "trend_maturity" "text",
    "fv_low" numeric,
    "fv_high" numeric,
    "price_location" "text",
    "dist_fv_pct" numeric,
    "struct_edge" "text",
    "entry_timing_type" "text",
    "entry_ready" boolean,
    "in_position" boolean,
    "reentry_mode" "text",
    "lifecycle" "text",
    "expected_r" numeric,
    "validity_score" numeric,
    "exec_eligibility" "text",
    "entry_zone_low" numeric,
    "entry_zone_high" numeric,
    "entry_mode" "text",
    "dist_entry_pct" numeric,
    "entry_action" "text",
    "opp_type" "text",
    "base_rank" integer,
    "base_score" numeric,
    "event_bias" "text",
    "event_sectors" "text",
    "upcoming_news" "text",
    "is_ipo" boolean,
    "trade_allowed" boolean,
    "suggested" "text",
    "pos_type" "text",
    "notes" "text",
    "position_state" "text",
    "ai_conviction" "text",
    "ai_conviction_reason" "text",
    "ai_risks" "jsonb",
    "ai_suggested_action" "text",
    "ai_note" "text",
    "ai_provider" "text",
    "ai_fallback_used" boolean,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "ai_shortlist_rank" integer,
    "ai_shortlist_reason" "text",
    "force_include" boolean DEFAULT false,
    "engines_list" "text",
    "convergence_pts" numeric,
    "eap_adjustment" numeric,
    "holding_score" numeric,
    "momentum_score" numeric,
    "institutional_score" numeric,
    "breakout_readiness" numeric,
    "risk_score" numeric,
    "days_to_trigger_est" integer,
    "ma_alignment_score" integer,
    "ha_signal" "text",
    "bb_squeeze" boolean,
    "bb_position_pct" numeric,
    "bb_context" "text",
    "bb_width_pct" numeric,
    "macd_direction" "text",
    "psar_dual_confirmed" boolean,
    "st_cushion_pct" numeric,
    "vwap_alignment" "text",
    "volume_trend" "text",
    "weekly_structure" "text",
    "fundamental_quality" "text",
    "stoch_context" "text",
    "active_regime" "text",
    "rsi_extended_thresh" numeric,
    "low_liquidity" boolean,
    "persistent_phase" "text",
    "macd_crossing_up" boolean,
    "dist_vwap_20d_pct" numeric,
    "liquidity_quality" "text",
    "fii_flag" "text",
    "compute_source" "text",
    "computed_at" timestamp with time zone,
    "composite_score" numeric,
    "priority_rank" integer,
    "sector_rank" integer,
    "screener_source" "text",
    "engine_scores_json" "text",
    "eap_revival" boolean,
    "ma_levels" "text",
    "engines_count" integer,
    "market_cap" numeric,
    "consol_range" "text",
    "dist_sma50" double precision,
    "low_30d" double precision,
    "low_52w" double precision,
    "pct_change" double precision,
    "ret_12m" double precision,
    "ret_3m" double precision
);


ALTER TABLE "public"."master_shortlist" OWNER TO "postgres";


COMMENT ON TABLE "public"."master_shortlist" IS '43-column ranked shortlist from MASTER_SHORTLIST Sheet tab';



COMMENT ON COLUMN "public"."master_shortlist"."force_include" IS 'Manual override: if TRUE, symbol always included regardless of screener output.';



COMMENT ON COLUMN "public"."master_shortlist"."holding_score" IS '0-100: Should I STAY in this position? Separate from validity_score (should I ENTER?).';



COMMENT ON COLUMN "public"."master_shortlist"."bb_context" IS 'Bollinger Band context: SQUEEZE_BULLISH | RIDING_UPPER | UPPER_HALF | MIDDLE | NEAR_LOWER | SQUEEZE_AT_LOW';



COMMENT ON COLUMN "public"."master_shortlist"."vwap_alignment" IS 'Multi-timeframe VWAP: ABOVE_ALL | ABOVE_ALL_EXTENDED | ABOVE_20D | BELOW_ALL | MIXED';



CREATE TABLE IF NOT EXISTS "public"."ml_training_log" (
    "id" bigint NOT NULL,
    "trained_at" timestamp with time zone DEFAULT "now"(),
    "trades_used" integer,
    "features_used" "jsonb",
    "accuracy" numeric,
    "precision_score" numeric,
    "recall_score" numeric,
    "model_version" "text",
    "notes" "text",
    "model_type" "text",
    "training_samples" integer,
    "feature_names" "text",
    "feature_importance" "text"
);


ALTER TABLE "public"."ml_training_log" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."ml_training_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."ml_training_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."ml_training_log_id_seq" OWNED BY "public"."ml_training_log"."id";



CREATE TABLE IF NOT EXISTS "public"."msl_computed" (
    "date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "composite_score" numeric,
    "base_score" numeric,
    "convergence_pts" numeric,
    "eap_adjustment" numeric,
    "engines_list" "text",
    "priority_rank" integer,
    "sector_rank" integer,
    "screener_source" "text",
    "final_score" numeric,
    "momentum_state" "text",
    "momentum_phase" "text",
    "velocity_state" "text",
    "trend_maturity" "text",
    "lifecycle" "text",
    "struct_edge" "text",
    "reentry_mode" "text",
    "entry_zone_low" numeric,
    "entry_zone_high" numeric,
    "entry_timing_type" "text",
    "price_location" "text",
    "dist_fv_pct" numeric,
    "dist_entry_pct" numeric,
    "validity_score" numeric,
    "expected_r" numeric,
    "in_position" boolean,
    "days_in_list" integer,
    "rank_vel_3d" numeric,
    "score_vel_5d" numeric,
    "holding_score" numeric,
    "momentum_score" numeric,
    "institutional_score" numeric,
    "breakout_readiness" numeric,
    "risk_score" numeric,
    "days_to_trigger_est" integer,
    "ma_alignment_score" integer,
    "ha_signal" "text",
    "bb_squeeze" boolean,
    "bb_position_pct" numeric,
    "bb_context" "text",
    "bb_width_pct" numeric,
    "macd_direction" "text",
    "psar_dual_confirmed" boolean,
    "st_cushion_pct" numeric,
    "vwap_alignment" "text",
    "volume_trend" "text",
    "weekly_structure" "text",
    "fundamental_quality" "text",
    "stoch_context" "text",
    "active_regime" "text",
    "rsi_extended_thresh" numeric,
    "company_name" "text",
    "sector" "text",
    "strategy_source" "text",
    "current_price" numeric,
    "compute_source" "text",
    "computed_at" "text",
    "engine_scores_json" "text",
    "eap_revival" boolean DEFAULT false,
    "trade_allowed" boolean DEFAULT true,
    "fii_flag" "text",
    "low_liquidity" boolean DEFAULT false,
    "liquidity_quality" "text",
    "dist_vwap_20d_pct" numeric(8,2),
    "macd_crossing_up" boolean DEFAULT false,
    "persistent_phase" "text",
    "ma_levels" "text"
);


ALTER TABLE "public"."msl_computed" OWNER TO "postgres";


COMMENT ON TABLE "public"."msl_computed" IS 'Joint output of screen_stocks (screener metadata) and compute_msl (intelligence fields). composite_score = screener aggregate. final_score = intelligence-weighted composite. These are different concepts and must NOT be confused. Serves as safe shadow target during transition from Sheet MSL.';



COMMENT ON COLUMN "public"."msl_computed"."composite_score" IS 'Written by screen_stocks.py. Screener aggregate: avg engine score + convergence bonus + EAP adjustment.';



COMMENT ON COLUMN "public"."msl_computed"."final_score" IS 'Written by compute_msl.py. Intelligence-weighted composite: momentum + validity + institutional + breakout - risk penalty.';



CREATE TABLE IF NOT EXISTS "public"."msl_history" (
    "snapshot_date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "company_name" "text",
    "priority_rank" integer,
    "sector" "text",
    "strategy_source" "text",
    "close_price" numeric,
    "price_location" "text",
    "dist_fv_pct" numeric,
    "entry_timing_type" "text",
    "momentum_phase" "text",
    "velocity_state" "text",
    "trend_maturity" "text",
    "struct_edge" "text",
    "in_position" boolean,
    "reentry_mode" "text",
    "lifecycle" "text",
    "expected_r" numeric,
    "validity_score" numeric,
    "final_score" numeric,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "breakout_readiness" numeric,
    "persistent_phase" "text",
    "momentum_state" "text",
    "holding_score" numeric,
    "risk_score" numeric
);


ALTER TABLE "public"."msl_history" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."nifty_total_market" (
    "symbol" "text" NOT NULL,
    "company_name" "text",
    "industry" "text",
    "isin" "text",
    "nifty_200" boolean,
    "nifty_500" boolean
);


ALTER TABLE "public"."nifty_total_market" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."nifty_upcoming_events" (
    "id" bigint NOT NULL,
    "symbol" "text",
    "company_name" "text",
    "purpose" "text",
    "details" "text",
    "event_date" "date",
    "days_to_event" integer,
    "source" "text" DEFAULT 'SHEET'::"text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."nifty_upcoming_events" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."nifty_upcoming_events_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."nifty_upcoming_events_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."nifty_upcoming_events_id_seq" OWNED BY "public"."nifty_upcoming_events"."id";



CREATE TABLE IF NOT EXISTS "public"."nse_holidays" (
    "date" "date" NOT NULL,
    "occasion" "text"
);


ALTER TABLE "public"."nse_holidays" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."open_positions" (
    "symbol" "text" NOT NULL,
    "company_name" "text",
    "sector" "text",
    "strategy" "text",
    "entry_date" "date",
    "entry_price" numeric,
    "proposed_qty" numeric,
    "actual_qty" numeric,
    "invested_value" numeric,
    "current_price" numeric,
    "current_value" numeric,
    "unrealized_pnl" numeric,
    "pnl_pct" numeric,
    "locked_profit" numeric,
    "lifecycle" "text",
    "sl_type" "text",
    "initial_sl_atr" numeric,
    "high_water_mark" numeric,
    "active_sl" numeric,
    "exit_signal" "text",
    "action_required" "text",
    "event_risk" "text",
    "upcoming_news" "text",
    "target_1" numeric,
    "target_2" numeric,
    "target_3" numeric,
    "status" "text" DEFAULT 'ACTIVE'::"text",
    "synced_at" timestamp with time zone DEFAULT "now"(),
    "kite_qty" integer,
    "reconcile_status" "text",
    "last_reconciled_at" timestamp with time zone,
    "sl_breach_alerted" boolean DEFAULT false,
    "sl_proximity_alerted" boolean DEFAULT false,
    "target_price" numeric,
    "target_pct" numeric,
    "target_hit" boolean DEFAULT false,
    "target_hit_at" timestamp with time zone,
    "trailing_sl_pct" numeric,
    "signal_id" bigint,
    "signal_date" "date",
    "signal_subtype" "text",
    "original_qty" integer,
    "current_qty" integer,
    "partial_bookings" "jsonb"
);


ALTER TABLE "public"."open_positions" OWNER TO "postgres";


COMMENT ON TABLE "public"."open_positions" IS 'Live positions from OPEN_POSITIONS Sheet tab (authoritative)';



CREATE TABLE IF NOT EXISTS "public"."order_history" (
    "id" bigint NOT NULL,
    "order_date" "date" DEFAULT CURRENT_DATE NOT NULL,
    "symbol" "text" NOT NULL,
    "signal_id" bigint,
    "broker_order_id" "text",
    "order_type" "text",
    "qty_requested" integer,
    "qty_executed" integer,
    "price_requested" numeric,
    "price_executed" numeric,
    "slippage_pct" numeric,
    "status" "text",
    "rejection_reason" "text",
    "kite_response" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."order_history" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."order_history_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."order_history_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."order_history_id_seq" OWNED BY "public"."order_history"."id";



CREATE TABLE IF NOT EXISTS "public"."performance_metrics" (
    "id" bigint NOT NULL,
    "metric_date" "date" NOT NULL,
    "grain" "text" NOT NULL,
    "signals_generated" integer,
    "signals_prime" integer,
    "signals_breakout" integer,
    "signals_staged" integer,
    "signals_reentry" integer,
    "win_rate_overall" numeric,
    "win_rate_prime" numeric,
    "win_rate_breakout" numeric,
    "win_rate_staged" numeric,
    "win_rate_reentry" numeric,
    "avg_fwd_ret_5d" numeric,
    "avg_fwd_ret_10d" numeric,
    "avg_fwd_ret_20d" numeric,
    "engine_stats" "jsonb",
    "score_return_corr" numeric,
    "holding_score_corr" numeric,
    "breakout_readiness_corr" numeric,
    "regime_stats" "jsonb",
    "ai_high_conv_wr" numeric,
    "ai_medium_conv_wr" numeric,
    "ai_low_conv_wr" numeric,
    "brain_proposals_applied" integer DEFAULT 0,
    "brain_wr_delta_avg" numeric,
    "market_regime" "text",
    "nifty_5d_ret" numeric,
    "india_vix_avg" numeric,
    "fii_net_5d" numeric,
    "outcome_coverage_pct" numeric,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."performance_metrics" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."performance_metrics_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."performance_metrics_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."performance_metrics_id_seq" OWNED BY "public"."performance_metrics"."id";



CREATE TABLE IF NOT EXISTS "public"."price_history_yf" (
    "symbol" "text" NOT NULL,
    "date" "date" NOT NULL,
    "open" numeric,
    "high" numeric,
    "low" numeric,
    "close" numeric,
    "volume" bigint,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."price_history_yf" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."raw_prices" (
    "date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "open" numeric,
    "high" numeric,
    "low" numeric,
    "close" numeric,
    "prev_close" numeric,
    "volume" bigint,
    "value_cr" numeric,
    "delivery_pct" numeric,
    "delivery_qty" bigint,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."raw_prices" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."regime_history" (
    "date" "date" NOT NULL,
    "regime" "text" NOT NULL,
    "nifty_price" numeric,
    "nifty_50dma" numeric,
    "nifty_200dma" numeric,
    "nifty_weekly_rsi" numeric,
    "india_vix" numeric,
    "avg_sector_breadth" numeric,
    "ctl_enabled" boolean,
    "sbs_enabled" boolean,
    "tpo_enabled" boolean,
    "eap_enabled" boolean,
    "regime_score" double precision,
    "nifty_1d_chg_pct" double precision,
    "nifty_5d_chg_pct" double precision,
    "nifty_20d_chg_pct" double precision,
    "advance_decline_ratio" double precision,
    "above_200dma_pct" double precision,
    "computed_regime" "text",
    "regime_score_computed" double precision,
    "vix_5d_delta" double precision,
    "midcap_breadth" double precision,
    "smallcap_breadth" double precision,
    "gift_premium_pct" double precision,
    "sp500_5d_ret" double precision,
    "usdinr_5d_chg" double precision,
    "banknifty_weekly_rsi" double precision
);


ALTER TABLE "public"."regime_history" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."safety_lists" (
    "symbol" "text" NOT NULL,
    "list_type" "text" NOT NULL,
    "stage" "text",
    "reason" "text",
    "effective_date" "date",
    "source" "text" DEFAULT 'NSE'::"text",
    "updated_at" timestamp with time zone DEFAULT "now"(),
    "listed_date" "date",
    "ingested_at" timestamp with time zone
);


ALTER TABLE "public"."safety_lists" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."scanner_signals" (
    "id" bigint NOT NULL,
    "date" "date",
    "symbol" "text",
    "pattern" "text",
    "confidence" numeric,
    "details" "jsonb",
    "in_msl" boolean,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "pattern_type" "text"
);


ALTER TABLE "public"."scanner_signals" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."scanner_signals_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."scanner_signals_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."scanner_signals_id_seq" OWNED BY "public"."scanner_signals"."id";



CREATE TABLE IF NOT EXISTS "public"."screener_performance" (
    "id" bigint NOT NULL,
    "symbol" "text" NOT NULL,
    "entry_date" "date" NOT NULL,
    "exit_date" "date" NOT NULL,
    "hold_days" integer NOT NULL,
    "entry_price" numeric,
    "exit_price" numeric,
    "pct_return" numeric,
    "composite_score" numeric,
    "priority_rank" integer,
    "engines_list" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."screener_performance" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."screener_performance_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."screener_performance_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."screener_performance_id_seq" OWNED BY "public"."screener_performance"."id";



CREATE TABLE IF NOT EXISTS "public"."script_change_log" (
    "id" bigint NOT NULL,
    "proposal_id" bigint,
    "script_path" "text" NOT NULL,
    "change_type" "text" NOT NULL,
    "diff_text" "text" NOT NULL,
    "backup_content" "text" NOT NULL,
    "git_commit_hash" "text",
    "applied_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "applied_by" "text" DEFAULT 'brain_engine'::"text" NOT NULL,
    "rolled_back_at" timestamp with time zone,
    "rolled_back_by" "text",
    "rollback_commit" "text",
    "test_result" "jsonb",
    "notes" "text"
);


ALTER TABLE "public"."script_change_log" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."script_change_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."script_change_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."script_change_log_id_seq" OWNED BY "public"."script_change_log"."id";



CREATE TABLE IF NOT EXISTS "public"."sector_strength" (
    "date" "date" NOT NULL,
    "sector" "text" NOT NULL,
    "stock_count" integer,
    "avg_rsi_daily" numeric,
    "avg_rsi_weekly" numeric,
    "avg_rsi_monthly" numeric,
    "avg_ret_6m" numeric,
    "breadth_sma50" numeric,
    "rank" integer,
    "top4_flag" boolean,
    "sector_state" "text",
    "fii_flow_sector" numeric
);


ALTER TABLE "public"."sector_strength" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."shadow_trades" (
    "id" bigint NOT NULL,
    "signal_id" bigint,
    "symbol" "text" NOT NULL,
    "strategy" "text",
    "action" "text",
    "entry_price" numeric,
    "qty" integer,
    "approved_at" timestamp with time zone DEFAULT "now"(),
    "would_execute" boolean DEFAULT false,
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."shadow_trades" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."shadow_trades_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."shadow_trades_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."shadow_trades_id_seq" OWNED BY "public"."shadow_trades"."id";



CREATE TABLE IF NOT EXISTS "public"."signal_daily_summary" (
    "date" "date" NOT NULL,
    "buy_raw" integer DEFAULT 0,
    "watch_raw" integer DEFAULT 0,
    "exit_raw" integer DEFAULT 0,
    "tier1" integer DEFAULT 0,
    "tier2" integer DEFAULT 0,
    "tier3" integer DEFAULT 0,
    "open_positions" integer DEFAULT 0,
    "step19_ok" boolean DEFAULT false,
    "guidance_ok" boolean DEFAULT false,
    "zero_reason" "text",
    "sizing" "text",
    "ai_note" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."signal_daily_summary" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."signal_log" (
    "id" bigint NOT NULL,
    "date" "date",
    "symbol" "text",
    "company_name" "text",
    "sector" "text",
    "strategy" "text",
    "signal_type" "text",
    "position_state" "text",
    "score" numeric,
    "in_rule_engine" boolean,
    "in_scanner" boolean,
    "eap_action" "text",
    "regime" "text",
    "regime_warning" boolean DEFAULT false,
    "asm_flag" boolean DEFAULT false,
    "fo_ban_flag" boolean DEFAULT false,
    "ai_conviction" "text",
    "ai_suggested_action" "text",
    "ai_provider" "text",
    "ai_fallback_used" boolean DEFAULT false,
    "fii_flag" "text",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "industry" "text",
    "industry_top5" boolean DEFAULT false,
    "industry_avg_rsi" numeric,
    "ai_note" "text",
    "ai_conviction_reason" "text",
    "ai_confidence" double precision,
    "rsi_daily" double precision,
    "rsi_weekly" double precision,
    "adx" double precision,
    "vol_ratio" double precision,
    "delivery_pct" double precision,
    "atr_pct" double precision,
    "ret_6m" double precision,
    "dist_sma50" double precision,
    "days_in_list" integer,
    "ai_strategy_validation" "text",
    "signal_subtype" "text",
    "score_adjusted" numeric,
    "sheet_conflict" boolean DEFAULT false,
    "rsi_monthly" double precision,
    "rs_vs_nifty" double precision,
    "consol_range" double precision,
    "ret_1m" double precision,
    "ret_3m" double precision,
    "above_sma50" boolean,
    "breakout_setup" boolean,
    "validity_score" double precision,
    "expected_r_msl" double precision,
    "trend_maturity" "text",
    "velocity_state" "text",
    "momentum_phase" "text",
    "days_to_trigger_est" integer,
    "sector_rank_at_entry" integer,
    "scanner_patterns" "text",
    "ai_catalyst" "text",
    "ai_risks" "text"[],
    "india_vix" double precision,
    "nifty_5d_chg_pct" double precision,
    "above_200dma_pct" double precision,
    "fii_net_20d_ctx" double precision,
    "filter_reason" "text",
    "holding_score" numeric,
    "ma_alignment_score" integer,
    "bb_context" "text",
    "vwap_alignment" "text",
    "weekly_structure" "text",
    "psar_dual_confirmed" boolean,
    "momentum_score" numeric,
    "institutional_score" numeric,
    "breakout_readiness" numeric,
    "risk_score" numeric,
    "volume_trend" "text",
    "fundamental_quality" "text",
    "active_regime" "text",
    "bb_squeeze" boolean,
    "bb_position_pct" numeric,
    "macd_direction" "text",
    "macd_crossing_up" boolean,
    "stoch_context" "text",
    "ha_signal" "text",
    "liquidity_quality" "text",
    "low_liquidity" boolean,
    "persistent_phase" "text",
    "rsi_extended_thresh" numeric,
    "dist_vwap_20d_pct" numeric,
    "st_cushion_pct" numeric,
    "reentry_mode" "text",
    "entry_timing_type" "text",
    "lifecycle" "text",
    "momentum_state" "text",
    "struct_edge" "text",
    "near_miss_data" "jsonb",
    "industry_rank" integer,
    "industry_state" "text",
    "sheet_conflict_type" "text"
);


ALTER TABLE "public"."signal_log" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."signal_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."signal_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."signal_log_id_seq" OWNED BY "public"."signal_log"."id";



CREATE TABLE IF NOT EXISTS "public"."signal_outcomes" (
    "id" bigint NOT NULL,
    "signal_date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "sector" "text",
    "strategy" "text",
    "signal_type" "text",
    "ai_tier" "text",
    "ai_conviction" "text",
    "score_adjusted" numeric,
    "ret_fwd_5d" numeric,
    "ret_fwd_10d" numeric,
    "ret_fwd_20d" numeric,
    "outcome_label" "text",
    "position_taken" boolean DEFAULT false,
    "actual_pnl_pct" numeric
);


ALTER TABLE "public"."signal_outcomes" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."signal_outcomes_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."signal_outcomes_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."signal_outcomes_id_seq" OWNED BY "public"."signal_outcomes"."id";



CREATE TABLE IF NOT EXISTS "public"."stock_data_daily" (
    "date" "date" NOT NULL,
    "symbol" "text" NOT NULL,
    "company_name" "text",
    "index_membership" "text",
    "sector" "text",
    "industry" "text",
    "market_cap" numeric,
    "market_cap_category" "text",
    "current_price" numeric,
    "open" numeric,
    "high" numeric,
    "low" numeric,
    "close" numeric,
    "high_52w" numeric,
    "low_52w" numeric,
    "high_30d" numeric,
    "low_30d" numeric,
    "close_30d" numeric,
    "price_6m_ago" numeric,
    "price_12m_ago" numeric,
    "ret_1w" numeric,
    "ret_1m" numeric,
    "ret_3m" numeric,
    "ret_6m" numeric,
    "ret_12m" numeric,
    "sma_10" numeric,
    "sma_20" numeric,
    "sma_50" numeric,
    "sma_200" numeric,
    "ema_10" numeric,
    "ema_20" numeric,
    "ema_50" numeric,
    "rsi_daily" numeric,
    "rsi_weekly" numeric,
    "rsi_monthly" numeric,
    "adx" numeric,
    "di_plus" numeric,
    "di_minus" numeric,
    "volume" bigint,
    "avg_vol_20d" bigint,
    "avg_vol_50d" bigint,
    "vwap" numeric,
    "vwap_20d" numeric,
    "vwap_50d" numeric,
    "pct_change" numeric,
    "atr_14" numeric,
    "atr_pct" numeric,
    "ha_high" numeric,
    "ha_low" numeric,
    "ha_close" numeric,
    "supertrend" numeric,
    "macd_line" numeric,
    "macd_signal" numeric,
    "macd_hist" numeric,
    "psar" numeric,
    "bb_upper" numeric,
    "bb_lower" numeric,
    "stoch" numeric,
    "ttm_net_profit" numeric,
    "net_profit_yearly" numeric,
    "eps" numeric,
    "quarterly_net_profit" numeric,
    "quarterly_variance" numeric,
    "above_sma50" boolean,
    "sma50_gt_200" boolean,
    "above_st" boolean,
    "wk_hi_high" boolean,
    "wk_hi_low" boolean,
    "dist_sma50" numeric,
    "vol_ratio" numeric,
    "value_cr" numeric,
    "delivery_pct" numeric,
    "consol_range" numeric,
    "breakout_setup" boolean,
    "bk_trigger" boolean,
    "upcoming_events" "text",
    "upcoming_event_type" "text",
    "in_master_shortlist" boolean,
    "rs_vs_nifty" numeric,
    "fii_sector_flow" numeric,
    "asm_flag" boolean DEFAULT false,
    "fo_ban_flag" boolean DEFAULT false,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "kite_price" numeric,
    "predicted_regime" "text",
    "compute_meta" "jsonb",
    "price_location" numeric,
    "delivery_qty" bigint
);


ALTER TABLE "public"."stock_data_daily" OWNER TO "postgres";


COMMENT ON TABLE "public"."stock_data_daily" IS '78-column stock universe ingested from STOCK_DATA Sheet tab';



CREATE TABLE IF NOT EXISTS "public"."strategy_config" (
    "strategy" "text" NOT NULL,
    "params" "jsonb" NOT NULL,
    "enabled" boolean DEFAULT true,
    "updated_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."strategy_config" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."system_config" (
    "key" "text" NOT NULL,
    "value" "text",
    "description" "text",
    "updated_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."system_config" OWNER TO "postgres";


COMMENT ON TABLE "public"."system_config" IS 'All system parameters configurable from frontend Settings tab';



CREATE OR REPLACE VIEW "public"."v_blocked_candidates" AS
 WITH "latest_msl" AS (
         SELECT DISTINCT ON ("master_shortlist"."symbol") "master_shortlist"."symbol",
            "master_shortlist"."date",
            "master_shortlist"."final_score",
            "master_shortlist"."composite_score",
            "master_shortlist"."sector",
            "master_shortlist"."strategy_source",
            "master_shortlist"."current_price",
            "master_shortlist"."dist_entry_pct",
            "master_shortlist"."entry_zone_low",
            "master_shortlist"."entry_zone_high",
            "master_shortlist"."active_regime",
            "master_shortlist"."lifecycle",
            "master_shortlist"."ai_conviction",
            "master_shortlist"."ai_note",
            "master_shortlist"."momentum_state"
           FROM "public"."master_shortlist"
          WHERE (("master_shortlist"."date" >= (CURRENT_DATE - 30)) AND ("master_shortlist"."final_score" >= (60)::numeric))
          ORDER BY "master_shortlist"."symbol", "master_shortlist"."date" DESC
        )
 SELECT "lm"."symbol",
    "lm"."date" AS "shortlist_date",
    "lm"."sector",
    "lm"."strategy_source",
    "lm"."final_score",
    "lm"."composite_score",
    "lm"."current_price",
    "lm"."dist_entry_pct",
    "lm"."entry_zone_low",
    "lm"."active_regime",
    "lm"."lifecycle",
    "lm"."ai_conviction",
    "lm"."ai_note",
    "lm"."momentum_state",
        CASE
            WHEN ("rh"."regime" = 'RISK OFF'::"text") THEN 'BLOCKED_REGIME'::"text"
            WHEN ("lm"."dist_entry_pct" > (5)::numeric) THEN 'TOO_FAR_FROM_ZONE'::"text"
            WHEN ("lm"."final_score" < (65)::numeric) THEN 'SCORE_TOO_LOW'::"text"
            ELSE 'FILTERED_PIPELINE'::"text"
        END AS "block_reason",
    "rh"."regime" AS "regime_on_date",
    "rh"."india_vix" AS "vix_on_date",
    'MSL_ONLY'::"text" AS "evaluation_source"
   FROM (("latest_msl" "lm"
     LEFT JOIN "public"."signal_log" "sl" ON ((("sl"."symbol" = "lm"."symbol") AND ("sl"."date" = "lm"."date"))))
     LEFT JOIN "public"."regime_history" "rh" ON (("rh"."date" = "lm"."date")))
  WHERE ("sl"."symbol" IS NULL)
  ORDER BY "lm"."date" DESC, "lm"."final_score" DESC;


ALTER VIEW "public"."v_blocked_candidates" OWNER TO "postgres";


CREATE OR REPLACE VIEW "public"."v_emerging_stocks" AS
 WITH "latest_msl" AS (
         SELECT DISTINCT ON ("master_shortlist"."symbol") "master_shortlist"."symbol",
            "master_shortlist"."date" AS "latest_date",
            "master_shortlist"."final_score",
            "master_shortlist"."composite_score",
            "master_shortlist"."sector",
            "master_shortlist"."strategy_source",
            "master_shortlist"."current_price",
            "master_shortlist"."dist_entry_pct",
            "master_shortlist"."entry_zone_low",
            "master_shortlist"."entry_zone_high",
            "master_shortlist"."expected_r",
            "master_shortlist"."days_in_list",
            "master_shortlist"."ai_conviction",
            "master_shortlist"."lifecycle",
            "master_shortlist"."momentum_state",
            "master_shortlist"."active_regime",
            "master_shortlist"."momentum_phase",
            "master_shortlist"."velocity_state"
           FROM "public"."master_shortlist"
          WHERE ("master_shortlist"."date" >= (CURRENT_DATE - 60))
          ORDER BY "master_shortlist"."symbol", "master_shortlist"."date" DESC
        ), "signal_hist" AS (
         SELECT "signal_log"."symbol",
            "count"(*) AS "total_signals",
            "max"("signal_log"."date") AS "last_signal_date",
            "max"("signal_log"."signal_type") AS "latest_signal_type",
            "avg"("signal_log"."score_adjusted") AS "avg_signal_score"
           FROM "public"."signal_log"
          WHERE ("signal_log"."date" >= (CURRENT_DATE - 60))
          GROUP BY "signal_log"."symbol"
        ), "outcome_hist" AS (
         SELECT "signal_outcomes"."symbol",
            "count"(*) FILTER (WHERE ("signal_outcomes"."outcome_label" = 'WIN'::"text")) AS "wins",
            "count"(*) FILTER (WHERE ("signal_outcomes"."outcome_label" = 'LOSS'::"text")) AS "losses",
            "round"("avg"("signal_outcomes"."ret_fwd_10d"), 2) AS "avg_ret_10d"
           FROM "public"."signal_outcomes"
          WHERE ("signal_outcomes"."signal_date" >= (CURRENT_DATE - 60))
          GROUP BY "signal_outcomes"."symbol"
        ), "latest_sdd" AS (
         SELECT DISTINCT ON ("stock_data_daily"."symbol") "stock_data_daily"."symbol",
            "stock_data_daily"."vol_ratio",
            "stock_data_daily"."delivery_pct",
            "stock_data_daily"."avg_vol_20d"
           FROM "public"."stock_data_daily"
          WHERE ("stock_data_daily"."date" >= (CURRENT_DATE - 5))
          ORDER BY "stock_data_daily"."symbol", "stock_data_daily"."date" DESC
        )
 SELECT "lm"."symbol",
    "lm"."sector",
    "lm"."strategy_source",
    "lm"."latest_date",
    "lm"."days_in_list",
    "lm"."final_score",
    "lm"."composite_score",
    "lm"."current_price",
    "lm"."dist_entry_pct",
    "lm"."entry_zone_low",
    "lm"."entry_zone_high",
    "lm"."expected_r",
    "lm"."ai_conviction",
    "lm"."lifecycle",
    "lm"."momentum_state",
    "lm"."momentum_phase",
    "lm"."velocity_state",
    "lm"."active_regime",
    "ls"."vol_ratio",
    "ls"."delivery_pct",
    "ls"."avg_vol_20d",
    "sh"."total_signals",
    "sh"."last_signal_date",
    "sh"."latest_signal_type",
    "round"("sh"."avg_signal_score", 1) AS "avg_signal_score",
    "oh"."wins",
    "oh"."losses",
    "oh"."avg_ret_10d",
        CASE
            WHEN ("lm"."days_in_list" <= 3) THEN 'NEW'::"text"
            WHEN ("lm"."days_in_list" <= 10) THEN 'EMERGING'::"text"
            WHEN ("lm"."final_score" > (75)::numeric) THEN 'ACCELERATING'::"text"
            WHEN ("lm"."days_in_list" > 30) THEN 'ESTABLISHED'::"text"
            ELSE 'DEVELOPING'::"text"
        END AS "emergence_tag",
        CASE
            WHEN ("sh"."total_signals" > 0) THEN true
            ELSE false
        END AS "has_signal"
   FROM ((("latest_msl" "lm"
     LEFT JOIN "signal_hist" "sh" ON (("sh"."symbol" = "lm"."symbol")))
     LEFT JOIN "outcome_hist" "oh" ON (("oh"."symbol" = "lm"."symbol")))
     LEFT JOIN "latest_sdd" "ls" ON (("ls"."symbol" = "lm"."symbol")))
  ORDER BY "lm"."days_in_list", "lm"."final_score" DESC;


ALTER VIEW "public"."v_emerging_stocks" OWNER TO "postgres";


CREATE OR REPLACE VIEW "public"."v_live_position_context" AS
 SELECT "op"."symbol",
    "op"."company_name",
    "op"."sector",
    "op"."strategy",
    "op"."lifecycle",
    "op"."entry_date",
    "op"."entry_price",
    "op"."actual_qty",
    "op"."invested_value",
    "op"."current_price" AS "position_price",
    "op"."unrealized_pnl",
    "op"."pnl_pct",
    "op"."active_sl",
    "op"."initial_sl_atr",
    "op"."high_water_mark",
    "op"."target_1",
    "op"."target_2",
    "op"."target_3",
    "op"."action_required",
    "op"."exit_signal",
    "op"."event_risk",
    "so"."ai_tier",
    "so"."ai_conviction" AS "signal_conviction",
    "so"."score_adjusted",
    "so"."signal_type" AS "original_signal_type",
    "so"."ret_fwd_5d",
    "so"."ret_fwd_10d",
    "so"."outcome_label",
    "ms"."final_score" AS "current_final_score",
    "ms"."composite_score" AS "current_composite_score",
    "ms"."entry_zone_low",
    "ms"."entry_zone_high",
    "ms"."dist_entry_pct",
    "ms"."expected_r",
    "ms"."ai_conviction" AS "current_ai_conviction",
    "ms"."ai_note" AS "current_ai_note",
    "ms"."active_regime",
    "ms"."momentum_state",
    "ms"."lifecycle" AS "msl_lifecycle",
    "rh_e"."regime" AS "regime_at_entry",
    "rh_e"."india_vix" AS "vix_at_entry",
    "rh_t"."regime" AS "regime_today",
    "rh_t"."india_vix" AS "vix_today"
   FROM (((("public"."open_positions" "op"
     LEFT JOIN "public"."signal_outcomes" "so" ON ((("so"."symbol" = "op"."symbol") AND ("so"."signal_date" = "op"."signal_date"))))
     LEFT JOIN LATERAL ( SELECT "master_shortlist"."date",
            "master_shortlist"."symbol",
            "master_shortlist"."company_name",
            "master_shortlist"."sector",
            "master_shortlist"."strategy_source",
            "master_shortlist"."current_price",
            "master_shortlist"."final_score",
            "master_shortlist"."days_in_list",
            "master_shortlist"."rank_vel_3d",
            "master_shortlist"."score_vel_5d",
            "master_shortlist"."momentum_state",
            "master_shortlist"."momentum_phase",
            "master_shortlist"."velocity_state",
            "master_shortlist"."trend_maturity",
            "master_shortlist"."fv_low",
            "master_shortlist"."fv_high",
            "master_shortlist"."price_location",
            "master_shortlist"."dist_fv_pct",
            "master_shortlist"."struct_edge",
            "master_shortlist"."entry_timing_type",
            "master_shortlist"."entry_ready",
            "master_shortlist"."in_position",
            "master_shortlist"."reentry_mode",
            "master_shortlist"."lifecycle",
            "master_shortlist"."expected_r",
            "master_shortlist"."validity_score",
            "master_shortlist"."exec_eligibility",
            "master_shortlist"."entry_zone_low",
            "master_shortlist"."entry_zone_high",
            "master_shortlist"."entry_mode",
            "master_shortlist"."dist_entry_pct",
            "master_shortlist"."entry_action",
            "master_shortlist"."opp_type",
            "master_shortlist"."base_rank",
            "master_shortlist"."base_score",
            "master_shortlist"."event_bias",
            "master_shortlist"."event_sectors",
            "master_shortlist"."upcoming_news",
            "master_shortlist"."is_ipo",
            "master_shortlist"."trade_allowed",
            "master_shortlist"."suggested",
            "master_shortlist"."pos_type",
            "master_shortlist"."notes",
            "master_shortlist"."position_state",
            "master_shortlist"."ai_conviction",
            "master_shortlist"."ai_conviction_reason",
            "master_shortlist"."ai_risks",
            "master_shortlist"."ai_suggested_action",
            "master_shortlist"."ai_note",
            "master_shortlist"."ai_provider",
            "master_shortlist"."ai_fallback_used",
            "master_shortlist"."created_at",
            "master_shortlist"."ai_shortlist_rank",
            "master_shortlist"."ai_shortlist_reason",
            "master_shortlist"."force_include",
            "master_shortlist"."engines_list",
            "master_shortlist"."convergence_pts",
            "master_shortlist"."eap_adjustment",
            "master_shortlist"."holding_score",
            "master_shortlist"."momentum_score",
            "master_shortlist"."institutional_score",
            "master_shortlist"."breakout_readiness",
            "master_shortlist"."risk_score",
            "master_shortlist"."days_to_trigger_est",
            "master_shortlist"."ma_alignment_score",
            "master_shortlist"."ha_signal",
            "master_shortlist"."bb_squeeze",
            "master_shortlist"."bb_position_pct",
            "master_shortlist"."bb_context",
            "master_shortlist"."bb_width_pct",
            "master_shortlist"."macd_direction",
            "master_shortlist"."psar_dual_confirmed",
            "master_shortlist"."st_cushion_pct",
            "master_shortlist"."vwap_alignment",
            "master_shortlist"."volume_trend",
            "master_shortlist"."weekly_structure",
            "master_shortlist"."fundamental_quality",
            "master_shortlist"."stoch_context",
            "master_shortlist"."active_regime",
            "master_shortlist"."rsi_extended_thresh",
            "master_shortlist"."low_liquidity",
            "master_shortlist"."persistent_phase",
            "master_shortlist"."macd_crossing_up",
            "master_shortlist"."dist_vwap_20d_pct",
            "master_shortlist"."liquidity_quality",
            "master_shortlist"."fii_flag",
            "master_shortlist"."compute_source",
            "master_shortlist"."computed_at",
            "master_shortlist"."composite_score",
            "master_shortlist"."priority_rank",
            "master_shortlist"."sector_rank",
            "master_shortlist"."screener_source",
            "master_shortlist"."engine_scores_json",
            "master_shortlist"."eap_revival",
            "master_shortlist"."ma_levels",
            "master_shortlist"."engines_count",
            "master_shortlist"."market_cap",
            "master_shortlist"."consol_range",
            "master_shortlist"."dist_sma50",
            "master_shortlist"."low_30d",
            "master_shortlist"."low_52w",
            "master_shortlist"."pct_change",
            "master_shortlist"."ret_12m",
            "master_shortlist"."ret_3m"
           FROM "public"."master_shortlist"
          WHERE ("master_shortlist"."symbol" = "op"."symbol")
          ORDER BY "master_shortlist"."date" DESC
         LIMIT 1) "ms" ON (true))
     LEFT JOIN "public"."regime_history" "rh_e" ON (("rh_e"."date" = "op"."entry_date")))
     LEFT JOIN "public"."regime_history" "rh_t" ON (("rh_t"."date" = CURRENT_DATE)));


ALTER VIEW "public"."v_live_position_context" OWNER TO "postgres";


CREATE OR REPLACE VIEW "public"."v_signal_daily_dashboard" AS
 WITH "raw_counts" AS (
         SELECT "signal_log"."date",
            "count"(*) FILTER (WHERE ("signal_log"."signal_type" = ANY (ARRAY['BUY_CANDIDATE'::"text", 'PRIME_SETUP'::"text", 'BREAKOUT_SETUP'::"text", 'REENTRY_SETUP'::"text", 'STAGED_ENTRY'::"text", 'MARKET_TOP_PICK'::"text"]))) AS "buy_signals_raw",
            "count"(*) FILTER (WHERE ("signal_log"."signal_type" = 'WATCH'::"text")) AS "watch_signals",
            "count"(*) FILTER (WHERE ("signal_log"."signal_type" = 'EXIT'::"text")) AS "exit_signals",
            "count"(*) FILTER (WHERE ("signal_log"."signal_type" = 'AVOID_ENTRY_EVENT'::"text")) AS "avoid_signals",
            "count"(DISTINCT "signal_log"."sector") FILTER (WHERE ("signal_log"."signal_type" = ANY (ARRAY['BUY_CANDIDATE'::"text", 'PRIME_SETUP'::"text", 'BREAKOUT_SETUP'::"text", 'REENTRY_SETUP'::"text", 'STAGED_ENTRY'::"text", 'MARKET_TOP_PICK'::"text"]))) AS "sectors_covered"
           FROM "public"."signal_log"
          GROUP BY "signal_log"."date"
        ), "ai_picks" AS (
         SELECT "ai_context"."date",
            "public"."safe_jsonb"("ai_context"."conviction_reason") AS "cr_json",
            "ai_context"."suggested_action" AS "sizing_recommendation",
            "ai_context"."ai_note" AS "portfolio_narrative"
           FROM "public"."ai_context"
          WHERE ("ai_context"."symbol" = '__FINAL_PICKS__'::"text")
        ), "ai_counts" AS (
         SELECT "p"."date",
            "jsonb_array_length"(("p"."cr_json" -> 'ranked_candidates'::"text")) AS "total_ranked",
            ( SELECT "count"(*) AS "count"
                   FROM "jsonb_array_elements"(("p"."cr_json" -> 'ranked_candidates'::"text")) "e"("value")
                  WHERE (("e"."value" ->> 'tier'::"text") = 'TIER_1'::"text")) AS "tier1_count",
            ( SELECT "count"(*) AS "count"
                   FROM "jsonb_array_elements"(("p"."cr_json" -> 'ranked_candidates'::"text")) "e"("value")
                  WHERE (("e"."value" ->> 'tier'::"text") = 'TIER_2'::"text")) AS "tier2_count",
            ( SELECT "count"(*) AS "count"
                   FROM "jsonb_array_elements"(("p"."cr_json" -> 'ranked_candidates'::"text")) "e"("value")
                  WHERE (("e"."value" ->> 'tier'::"text") = 'TIER_3'::"text")) AS "tier3_count",
            "p"."sizing_recommendation",
            "p"."portfolio_narrative",
                CASE
                    WHEN ("p"."cr_json" IS NULL) THEN true
                    ELSE false
                END AS "json_was_truncated"
           FROM "ai_picks" "p"
        )
 SELECT "r"."date",
    "r"."buy_signals_raw",
    "r"."watch_signals",
    "r"."exit_signals",
    "r"."avoid_signals",
    "r"."sectors_covered",
    COALESCE("a"."total_ranked", 0) AS "ai_ranked",
    COALESCE("a"."tier1_count", (0)::bigint) AS "tier1",
    COALESCE("a"."tier2_count", (0)::bigint) AS "tier2",
    COALESCE("a"."tier3_count", (0)::bigint) AS "tier3",
    "a"."json_was_truncated",
        CASE
            WHEN ("r"."buy_signals_raw" = 0) THEN 'NO_RAW_SIGNALS'::"text"
            WHEN (("a"."total_ranked" IS NULL) AND "a"."json_was_truncated") THEN 'STEP19_JSON_TRUNCATED'::"text"
            WHEN ("a"."total_ranked" IS NULL) THEN 'STEP19_MISSING'::"text"
            WHEN ((COALESCE("a"."tier1_count", (0)::bigint) = 0) AND (COALESCE("a"."tier2_count", (0)::bigint) = 0)) THEN 'AI_ALL_TIER3'::"text"
            ELSE 'OK'::"text"
        END AS "status",
    "a"."sizing_recommendation",
    "left"("a"."portfolio_narrative", 50000) AS "ai_narrative"
   FROM ("raw_counts" "r"
     LEFT JOIN "ai_counts" "a" ON (("r"."date" = "a"."date")))
  ORDER BY "r"."date" DESC;


ALTER VIEW "public"."v_signal_daily_dashboard" OWNER TO "postgres";


CREATE OR REPLACE VIEW "public"."v_signal_performance" AS
 SELECT "so"."signal_date",
    "so"."symbol",
    "so"."sector",
    "so"."strategy",
    "so"."signal_type",
    "so"."ai_tier",
    "so"."ai_conviction",
    "so"."score_adjusted",
    "so"."ret_fwd_5d",
    "so"."ret_fwd_10d",
    "so"."ret_fwd_20d",
    "so"."outcome_label",
    "so"."position_taken",
    "so"."actual_pnl_pct",
        CASE
            WHEN (("so"."position_taken" = false) AND ("so"."ret_fwd_10d" > 3.0) AND ("so"."ai_tier" = ANY (ARRAY['TIER_1'::"text", 'TIER_2'::"text"]))) THEN true
            ELSE false
        END AS "missed_opportunity",
        CASE
            WHEN ("so"."position_taken" = false) THEN "so"."ret_fwd_10d"
            ELSE NULL::numeric
        END AS "opportunity_cost_pct",
    "ms"."entry_zone_low",
    "ms"."entry_zone_high",
    "ms"."final_score" AS "msl_final_score",
    "ms"."expected_r",
    "ms"."strategy_source",
    "rh"."regime" AS "regime_at_signal",
    "rh"."india_vix" AS "vix_at_signal",
    "rh"."nifty_5d_chg_pct" AS "nifty_5d_at_signal",
    "pm"."win_rate_overall" AS "day_win_rate",
    "pm"."avg_fwd_ret_10d" AS "day_avg_ret_10d",
    "pm"."outcome_coverage_pct" AS "day_coverage_pct",
    "pm"."market_regime" AS "day_market_regime"
   FROM ((("public"."signal_outcomes" "so"
     LEFT JOIN LATERAL ( SELECT "master_shortlist"."entry_zone_low",
            "master_shortlist"."entry_zone_high",
            "master_shortlist"."final_score",
            "master_shortlist"."expected_r",
            "master_shortlist"."strategy_source"
           FROM "public"."master_shortlist"
          WHERE (("master_shortlist"."symbol" = "so"."symbol") AND ("master_shortlist"."date" = "so"."signal_date"))
         LIMIT 1) "ms" ON (true))
     LEFT JOIN "public"."regime_history" "rh" ON (("rh"."date" = "so"."signal_date")))
     LEFT JOIN "public"."performance_metrics" "pm" ON ((("pm"."metric_date" = "so"."signal_date") AND ("pm"."grain" = 'daily'::"text"))))
  ORDER BY "so"."signal_date" DESC, "so"."ai_tier";


ALTER VIEW "public"."v_signal_performance" OWNER TO "postgres";


ALTER TABLE ONLY "public"."ai_model_performance" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."ai_model_performance_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."brain_analysis_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."brain_analysis_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."brain_proposals" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."brain_proposals_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."brain_script_registry" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."brain_script_registry_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."chartink_raw_data" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."chartink_raw_data_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."closed_positions" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."closed_positions_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."config_change_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."config_change_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."data_anomalies" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."data_anomalies_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."event_calendar" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."event_calendar_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."evolution_proposals" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."evolution_proposals_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."industry_strength" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."industry_strength_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."lessons" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."lessons_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."macro_indicators" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."macro_indicators_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."market_news" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."market_news_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."ml_training_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."ml_training_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."nifty_upcoming_events" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nifty_upcoming_events_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."order_history" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."order_history_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."performance_metrics" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."performance_metrics_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."scanner_signals" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."scanner_signals_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."screener_performance" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."screener_performance_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."script_change_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."script_change_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."shadow_trades" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."shadow_trades_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."signal_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."signal_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."signal_outcomes" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."signal_outcomes_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."ai_context"
    ADD CONSTRAINT "ai_context_pkey" PRIMARY KEY ("date", "symbol");



ALTER TABLE ONLY "public"."ai_model_performance"
    ADD CONSTRAINT "ai_model_performance_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."brain_analysis_log"
    ADD CONSTRAINT "brain_analysis_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."brain_analysis_log"
    ADD CONSTRAINT "brain_analysis_log_run_id_key" UNIQUE ("run_id");



ALTER TABLE ONLY "public"."brain_proposals"
    ADD CONSTRAINT "brain_proposals_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."brain_script_registry"
    ADD CONSTRAINT "brain_script_registry_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."brain_script_registry"
    ADD CONSTRAINT "brain_script_registry_script_path_key" UNIQUE ("script_path");



ALTER TABLE ONLY "public"."chartink_raw_data"
    ADD CONSTRAINT "chartink_raw_data_date_symbol_key" UNIQUE ("date", "symbol");



ALTER TABLE ONLY "public"."chartink_raw_data"
    ADD CONSTRAINT "chartink_raw_data_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."closed_positions"
    ADD CONSTRAINT "closed_positions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."closed_positions"
    ADD CONSTRAINT "closed_positions_symbol_entry_date_exit_date_key" UNIQUE ("symbol", "entry_date", "exit_date");



ALTER TABLE ONLY "public"."config_change_log"
    ADD CONSTRAINT "config_change_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."custom_views"
    ADD CONSTRAINT "custom_views_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."data_anomalies"
    ADD CONSTRAINT "data_anomalies_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."event_calendar"
    ADD CONSTRAINT "event_calendar_event_name_start_date_key" UNIQUE ("event_name", "start_date");



ALTER TABLE ONLY "public"."event_calendar"
    ADD CONSTRAINT "event_calendar_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."evolution_proposals"
    ADD CONSTRAINT "evolution_proposals_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."fii_dii_flow"
    ADD CONSTRAINT "fii_dii_flow_pkey" PRIMARY KEY ("date");



ALTER TABLE ONLY "public"."global_cues"
    ADD CONSTRAINT "global_cues_pkey" PRIMARY KEY ("date", "session");



ALTER TABLE ONLY "public"."industry_strength"
    ADD CONSTRAINT "industry_strength_date_industry_key" UNIQUE ("date", "industry");



ALTER TABLE ONLY "public"."industry_strength"
    ADD CONSTRAINT "industry_strength_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."lessons"
    ADD CONSTRAINT "lessons_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."macro_indicators"
    ADD CONSTRAINT "macro_indicators_indicator_date_indicator_name_key" UNIQUE ("indicator_date", "indicator_name");



ALTER TABLE ONLY "public"."macro_indicators"
    ADD CONSTRAINT "macro_indicators_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."market_news"
    ADD CONSTRAINT "market_news_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."market_news"
    ADD CONSTRAINT "market_news_source_headline_key" UNIQUE ("source", "headline");



ALTER TABLE ONLY "public"."market_regime"
    ADD CONSTRAINT "market_regime_date_key" UNIQUE ("date");



ALTER TABLE ONLY "public"."market_regime"
    ADD CONSTRAINT "market_regime_pkey" PRIMARY KEY ("date");



ALTER TABLE ONLY "public"."master_shortlist"
    ADD CONSTRAINT "master_shortlist_pkey" PRIMARY KEY ("date", "symbol");



ALTER TABLE ONLY "public"."ml_training_log"
    ADD CONSTRAINT "ml_training_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."msl_computed"
    ADD CONSTRAINT "msl_computed_pkey" PRIMARY KEY ("date", "symbol");



ALTER TABLE ONLY "public"."msl_history"
    ADD CONSTRAINT "msl_history_pkey" PRIMARY KEY ("snapshot_date", "symbol");



ALTER TABLE ONLY "public"."nifty_total_market"
    ADD CONSTRAINT "nifty_total_market_pkey" PRIMARY KEY ("symbol");



ALTER TABLE ONLY "public"."nifty_upcoming_events"
    ADD CONSTRAINT "nifty_upcoming_events_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."nifty_upcoming_events"
    ADD CONSTRAINT "nifty_upcoming_events_symbol_purpose_event_date_key" UNIQUE ("symbol", "purpose", "event_date");



ALTER TABLE ONLY "public"."nse_holidays"
    ADD CONSTRAINT "nse_holidays_pkey" PRIMARY KEY ("date");



ALTER TABLE ONLY "public"."open_positions"
    ADD CONSTRAINT "open_positions_pkey" PRIMARY KEY ("symbol");



ALTER TABLE ONLY "public"."order_history"
    ADD CONSTRAINT "order_history_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."performance_metrics"
    ADD CONSTRAINT "perf_metrics_pkey" PRIMARY KEY ("metric_date", "grain");



ALTER TABLE ONLY "public"."price_history_yf"
    ADD CONSTRAINT "price_history_yf_pkey" PRIMARY KEY ("symbol", "date");



ALTER TABLE ONLY "public"."raw_prices"
    ADD CONSTRAINT "raw_prices_pkey" PRIMARY KEY ("date", "symbol");



ALTER TABLE ONLY "public"."regime_history"
    ADD CONSTRAINT "regime_history_date_key" UNIQUE ("date");



ALTER TABLE ONLY "public"."regime_history"
    ADD CONSTRAINT "regime_history_pkey" PRIMARY KEY ("date");



ALTER TABLE ONLY "public"."safety_lists"
    ADD CONSTRAINT "safety_lists_pkey" PRIMARY KEY ("symbol", "list_type");



ALTER TABLE ONLY "public"."scanner_signals"
    ADD CONSTRAINT "scanner_signals_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."screener_performance"
    ADD CONSTRAINT "screener_performance_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."screener_performance"
    ADD CONSTRAINT "screener_performance_symbol_entry_date_hold_days_key" UNIQUE ("symbol", "entry_date", "hold_days");



ALTER TABLE ONLY "public"."script_change_log"
    ADD CONSTRAINT "script_change_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."sector_strength"
    ADD CONSTRAINT "sector_strength_pkey" PRIMARY KEY ("date", "sector");



ALTER TABLE ONLY "public"."shadow_trades"
    ADD CONSTRAINT "shadow_trades_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."signal_daily_summary"
    ADD CONSTRAINT "signal_daily_summary_pkey" PRIMARY KEY ("date");



ALTER TABLE ONLY "public"."signal_log"
    ADD CONSTRAINT "signal_log_date_symbol_unique" UNIQUE ("date", "symbol");



ALTER TABLE ONLY "public"."signal_log"
    ADD CONSTRAINT "signal_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."signal_outcomes"
    ADD CONSTRAINT "signal_outcomes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."signal_outcomes"
    ADD CONSTRAINT "signal_outcomes_signal_date_symbol_key" UNIQUE ("signal_date", "symbol");



ALTER TABLE ONLY "public"."stock_data_daily"
    ADD CONSTRAINT "stock_data_daily_pkey" PRIMARY KEY ("date", "symbol");



ALTER TABLE ONLY "public"."strategy_config"
    ADD CONSTRAINT "strategy_config_pkey" PRIMARY KEY ("strategy");



ALTER TABLE ONLY "public"."system_config"
    ADD CONSTRAINT "system_config_pkey" PRIMARY KEY ("key");



CREATE INDEX "custom_views_tab_id_idx" ON "public"."custom_views" USING "btree" ("tab_id");



CREATE INDEX "idx_ai_context_date" ON "public"."ai_context" USING "btree" ("date", "symbol");



CREATE INDEX "idx_ai_model_perf_date" ON "public"."ai_model_performance" USING "btree" ("date" DESC);



CREATE INDEX "idx_ai_model_perf_provider" ON "public"."ai_model_performance" USING "btree" ("provider");



CREATE INDEX "idx_anomalies_check" ON "public"."data_anomalies" USING "btree" ("check_name", "date" DESC);



CREATE INDEX "idx_anomalies_date" ON "public"."data_anomalies" USING "btree" ("date" DESC);



CREATE INDEX "idx_anomalies_severity" ON "public"."data_anomalies" USING "btree" ("severity");



CREATE INDEX "idx_brain_proposals_created" ON "public"."brain_proposals" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_brain_proposals_run" ON "public"."brain_proposals" USING "btree" ("analysis_run_id");



CREATE INDEX "idx_brain_proposals_status" ON "public"."brain_proposals" USING "btree" ("status");



CREATE INDEX "idx_brain_proposals_target" ON "public"."brain_proposals" USING "btree" ("target_key");



CREATE INDEX "idx_ccl_date" ON "public"."config_change_log" USING "btree" ("changed_at" DESC);



CREATE INDEX "idx_ccl_key" ON "public"."config_change_log" USING "btree" ("key");



CREATE INDEX "idx_ccl_pid" ON "public"."config_change_log" USING "btree" ("proposal_id");



CREATE INDEX "idx_chartink_date" ON "public"."chartink_raw_data" USING "btree" ("date" DESC);



CREATE INDEX "idx_chartink_symbol" ON "public"."chartink_raw_data" USING "btree" ("symbol");



CREATE INDEX "idx_closed_symbol" ON "public"."closed_positions" USING "btree" ("symbol");



CREATE INDEX "idx_cp_date" ON "public"."closed_positions" USING "btree" ("exit_date" DESC);



CREATE INDEX "idx_cp_symbol" ON "public"."closed_positions" USING "btree" ("symbol");



CREATE INDEX "idx_events_active" ON "public"."event_calendar" USING "btree" ("is_active", "start_date");



CREATE INDEX "idx_events_sector" ON "public"."event_calendar" USING "btree" ("affected_sectors");



CREATE INDEX "idx_fii_date" ON "public"."fii_dii_flow" USING "btree" ("date" DESC);



CREATE INDEX "idx_industry_strength_date" ON "public"."industry_strength" USING "btree" ("date" DESC);



CREATE INDEX "idx_industry_strength_rank" ON "public"."industry_strength" USING "btree" ("rank");



CREATE INDEX "idx_lessons_active" ON "public"."lessons" USING "btree" ("is_active", "impacted_sector", "date" DESC);



CREATE INDEX "idx_macro_ind_date" ON "public"."macro_indicators" USING "btree" ("indicator_date" DESC);



CREATE INDEX "idx_macro_ind_name" ON "public"."macro_indicators" USING "btree" ("indicator_name");



CREATE INDEX "idx_market_news_category" ON "public"."market_news" USING "btree" ("category");



CREATE INDEX "idx_market_news_date" ON "public"."market_news" USING "btree" ("news_date" DESC);



CREATE INDEX "idx_market_news_impact" ON "public"."market_news" USING "btree" ("impact_type");



CREATE INDEX "idx_market_regime_predicted_at" ON "public"."market_regime" USING "btree" ("regime_predicted_at" DESC NULLS LAST);



CREATE INDEX "idx_msl_date" ON "public"."master_shortlist" USING "btree" ("date");



CREATE INDEX "idx_msl_hist" ON "public"."msl_history" USING "btree" ("snapshot_date", "symbol");



CREATE INDEX "idx_msl_score" ON "public"."master_shortlist" USING "btree" ("date", "final_score" DESC);



CREATE INDEX "idx_msl_state" ON "public"."master_shortlist" USING "btree" ("date", "position_state");



CREATE INDEX "idx_order_history_date" ON "public"."order_history" USING "btree" ("order_date" DESC);



CREATE INDEX "idx_order_history_symbol" ON "public"."order_history" USING "btree" ("symbol");



CREATE INDEX "idx_pm_date" ON "public"."performance_metrics" USING "btree" ("metric_date" DESC);



CREATE INDEX "idx_pm_grain" ON "public"."performance_metrics" USING "btree" ("grain", "metric_date" DESC);



CREATE INDEX "idx_price_history_symbol_date" ON "public"."price_history_yf" USING "btree" ("symbol", "date" DESC);



CREATE INDEX "idx_price_history_yf_date" ON "public"."price_history_yf" USING "btree" ("date");



CREATE INDEX "idx_price_history_yf_symbol" ON "public"."price_history_yf" USING "btree" ("symbol");



CREATE INDEX "idx_safety_lists_symbol" ON "public"."safety_lists" USING "btree" ("symbol");



CREATE INDEX "idx_safety_symbol" ON "public"."safety_lists" USING "btree" ("symbol");



CREATE INDEX "idx_scanner_date" ON "public"."scanner_signals" USING "btree" ("date");



CREATE INDEX "idx_scl_date" ON "public"."script_change_log" USING "btree" ("applied_at" DESC);



CREATE INDEX "idx_scl_pid" ON "public"."script_change_log" USING "btree" ("proposal_id");



CREATE INDEX "idx_scl_script" ON "public"."script_change_log" USING "btree" ("script_path");



CREATE INDEX "idx_sdd_compute_meta" ON "public"."stock_data_daily" USING "gin" ("compute_meta") WHERE ("compute_meta" IS NOT NULL);



CREATE INDEX "idx_shadow_trades_created" ON "public"."shadow_trades" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_signal_log_date" ON "public"."signal_log" USING "btree" ("date");



CREATE INDEX "idx_signal_log_symbol" ON "public"."signal_log" USING "btree" ("symbol");



CREATE INDEX "idx_signal_log_type" ON "public"."signal_log" USING "btree" ("date", "signal_type");



CREATE INDEX "idx_stock_data_date" ON "public"."stock_data_daily" USING "btree" ("date");



CREATE INDEX "idx_stock_data_symbol" ON "public"."stock_data_daily" USING "btree" ("symbol");



CREATE INDEX "idx_upcoming_date" ON "public"."nifty_upcoming_events" USING "btree" ("event_date");



CREATE INDEX "idx_upcoming_symbol" ON "public"."nifty_upcoming_events" USING "btree" ("symbol");



CREATE OR REPLACE TRIGGER "trg_ai_context_updated_at" BEFORE UPDATE ON "public"."ai_context" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "trg_msl_history_updated_at" BEFORE UPDATE ON "public"."msl_history" FOR EACH ROW EXECUTE FUNCTION "public"."set_msl_history_updated_at"();



CREATE OR REPLACE TRIGGER "trg_open_positions_updated_at" BEFORE UPDATE ON "public"."open_positions" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



ALTER TABLE ONLY "public"."config_change_log"
    ADD CONSTRAINT "config_change_log_proposal_id_fkey" FOREIGN KEY ("proposal_id") REFERENCES "public"."brain_proposals"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."script_change_log"
    ADD CONSTRAINT "script_change_log_proposal_id_fkey" FOREIGN KEY ("proposal_id") REFERENCES "public"."brain_proposals"("id") ON DELETE SET NULL;



ALTER TABLE "public"."ai_context" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."ai_model_performance" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "allow_read" ON "public"."ai_model_performance" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."brain_analysis_log" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."brain_proposals" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."closed_positions" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."custom_views" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."lessons" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."market_regime" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."master_shortlist" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."open_positions" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."performance_metrics" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."signal_log" FOR SELECT USING (true);



CREATE POLICY "allow_read" ON "public"."system_config" FOR SELECT USING (true);



CREATE POLICY "allow_read_custom_views" ON "public"."custom_views" FOR SELECT USING (true);



CREATE POLICY "anon_read_config" ON "public"."system_config" FOR SELECT USING (true);



CREATE POLICY "anon_read_events" ON "public"."event_calendar" FOR SELECT USING (true);



CREATE POLICY "anon_read_industry" ON "public"."industry_strength" FOR SELECT USING (true);



CREATE POLICY "anon_read_msl" ON "public"."master_shortlist" FOR SELECT USING (true);



CREATE POLICY "anon_read_open_pos" ON "public"."open_positions" FOR SELECT USING (true);



CREATE POLICY "anon_read_regime" ON "public"."market_regime" FOR SELECT USING (true);



CREATE POLICY "anon_read_sectors" ON "public"."sector_strength" FOR SELECT USING (true);



CREATE POLICY "anon_read_signals" ON "public"."signal_log" FOR SELECT USING (true);



CREATE POLICY "anon_read_stock_data" ON "public"."stock_data_daily" FOR SELECT USING (true);



CREATE POLICY "anon_read_strategy_cfg" ON "public"."strategy_config" FOR SELECT USING (true);



ALTER TABLE "public"."brain_analysis_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."brain_proposals" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."brain_script_registry" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."chartink_raw_data" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."closed_positions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."config_change_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."custom_views" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."data_anomalies" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."event_calendar" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."evolution_proposals" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."fii_dii_flow" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."global_cues" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."industry_strength" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."lessons" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."macro_indicators" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."market_news" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."market_regime" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."master_shortlist" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."ml_training_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."msl_computed" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."msl_history" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."nifty_total_market" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."nifty_upcoming_events" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."nse_holidays" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."open_positions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."order_history" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."performance_metrics" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."price_history_yf" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."raw_prices" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."regime_history" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."safety_lists" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."scanner_signals" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."screener_performance" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."script_change_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."sector_strength" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."shadow_trades" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."signal_daily_summary" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."signal_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."signal_outcomes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."stock_data_daily" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."strategy_config" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."system_config" ENABLE ROW LEVEL SECURITY;




ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";






ALTER PUBLICATION "supabase_realtime" ADD TABLE ONLY "public"."closed_positions";



ALTER PUBLICATION "supabase_realtime" ADD TABLE ONLY "public"."master_shortlist";



ALTER PUBLICATION "supabase_realtime" ADD TABLE ONLY "public"."open_positions";



GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "service_role";






















































































































































GRANT ALL ON FUNCTION "public"."get_public_tables"() TO "anon";
GRANT ALL ON FUNCTION "public"."get_public_tables"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_public_tables"() TO "service_role";



GRANT ALL ON FUNCTION "public"."get_symbol_history_summary"("p_symbols" "text"[], "p_cutoff" "date", "p_today" "date") TO "anon";
GRANT ALL ON FUNCTION "public"."get_symbol_history_summary"("p_symbols" "text"[], "p_cutoff" "date", "p_today" "date") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_symbol_history_summary"("p_symbols" "text"[], "p_cutoff" "date", "p_today" "date") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_table_columns"("p_table_name" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."get_table_columns"("p_table_name" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_table_columns"("p_table_name" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "anon";
GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "service_role";



GRANT ALL ON FUNCTION "public"."safe_jsonb"("txt" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."safe_jsonb"("txt" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."safe_jsonb"("txt" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "postgres";
GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "anon";
GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "service_role";



GRANT ALL ON FUNCTION "public"."set_msl_history_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."set_msl_history_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_msl_history_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."show_limit"() TO "postgres";
GRANT ALL ON FUNCTION "public"."show_limit"() TO "anon";
GRANT ALL ON FUNCTION "public"."show_limit"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."show_limit"() TO "service_role";



GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "postgres";
GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "anon";
GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "service_role";



GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "service_role";


















GRANT ALL ON TABLE "public"."ai_context" TO "anon";
GRANT ALL ON TABLE "public"."ai_context" TO "authenticated";
GRANT ALL ON TABLE "public"."ai_context" TO "service_role";



GRANT ALL ON TABLE "public"."ai_model_performance" TO "anon";
GRANT ALL ON TABLE "public"."ai_model_performance" TO "authenticated";
GRANT ALL ON TABLE "public"."ai_model_performance" TO "service_role";



GRANT ALL ON SEQUENCE "public"."ai_model_performance_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."ai_model_performance_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."ai_model_performance_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."brain_analysis_log" TO "anon";
GRANT ALL ON TABLE "public"."brain_analysis_log" TO "authenticated";
GRANT ALL ON TABLE "public"."brain_analysis_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."brain_analysis_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."brain_analysis_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."brain_analysis_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."brain_proposals" TO "anon";
GRANT ALL ON TABLE "public"."brain_proposals" TO "authenticated";
GRANT ALL ON TABLE "public"."brain_proposals" TO "service_role";



GRANT ALL ON SEQUENCE "public"."brain_proposals_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."brain_proposals_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."brain_proposals_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."brain_script_registry" TO "anon";
GRANT ALL ON TABLE "public"."brain_script_registry" TO "authenticated";
GRANT ALL ON TABLE "public"."brain_script_registry" TO "service_role";



GRANT ALL ON SEQUENCE "public"."brain_script_registry_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."brain_script_registry_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."brain_script_registry_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."chartink_raw_data" TO "anon";
GRANT ALL ON TABLE "public"."chartink_raw_data" TO "authenticated";
GRANT ALL ON TABLE "public"."chartink_raw_data" TO "service_role";



GRANT ALL ON SEQUENCE "public"."chartink_raw_data_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."chartink_raw_data_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."chartink_raw_data_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."closed_positions" TO "anon";
GRANT ALL ON TABLE "public"."closed_positions" TO "authenticated";
GRANT ALL ON TABLE "public"."closed_positions" TO "service_role";



GRANT ALL ON SEQUENCE "public"."closed_positions_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."closed_positions_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."closed_positions_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."config_change_log" TO "anon";
GRANT ALL ON TABLE "public"."config_change_log" TO "authenticated";
GRANT ALL ON TABLE "public"."config_change_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."config_change_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."config_change_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."config_change_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."custom_views" TO "anon";
GRANT ALL ON TABLE "public"."custom_views" TO "authenticated";
GRANT ALL ON TABLE "public"."custom_views" TO "service_role";



GRANT ALL ON TABLE "public"."data_anomalies" TO "anon";
GRANT ALL ON TABLE "public"."data_anomalies" TO "authenticated";
GRANT ALL ON TABLE "public"."data_anomalies" TO "service_role";



GRANT ALL ON SEQUENCE "public"."data_anomalies_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."data_anomalies_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."data_anomalies_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."event_calendar" TO "anon";
GRANT ALL ON TABLE "public"."event_calendar" TO "authenticated";
GRANT ALL ON TABLE "public"."event_calendar" TO "service_role";



GRANT ALL ON SEQUENCE "public"."event_calendar_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."event_calendar_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."event_calendar_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."evolution_proposals" TO "anon";
GRANT ALL ON TABLE "public"."evolution_proposals" TO "authenticated";
GRANT ALL ON TABLE "public"."evolution_proposals" TO "service_role";



GRANT ALL ON SEQUENCE "public"."evolution_proposals_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."evolution_proposals_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."evolution_proposals_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."fii_dii_flow" TO "anon";
GRANT ALL ON TABLE "public"."fii_dii_flow" TO "authenticated";
GRANT ALL ON TABLE "public"."fii_dii_flow" TO "service_role";



GRANT ALL ON TABLE "public"."global_cues" TO "anon";
GRANT ALL ON TABLE "public"."global_cues" TO "authenticated";
GRANT ALL ON TABLE "public"."global_cues" TO "service_role";



GRANT ALL ON TABLE "public"."industry_strength" TO "anon";
GRANT ALL ON TABLE "public"."industry_strength" TO "authenticated";
GRANT ALL ON TABLE "public"."industry_strength" TO "service_role";



GRANT ALL ON SEQUENCE "public"."industry_strength_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."industry_strength_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."industry_strength_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."lessons" TO "anon";
GRANT ALL ON TABLE "public"."lessons" TO "authenticated";
GRANT ALL ON TABLE "public"."lessons" TO "service_role";



GRANT ALL ON SEQUENCE "public"."lessons_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."lessons_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."lessons_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."macro_indicators" TO "anon";
GRANT ALL ON TABLE "public"."macro_indicators" TO "authenticated";
GRANT ALL ON TABLE "public"."macro_indicators" TO "service_role";



GRANT ALL ON SEQUENCE "public"."macro_indicators_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."macro_indicators_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."macro_indicators_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."market_news" TO "anon";
GRANT ALL ON TABLE "public"."market_news" TO "authenticated";
GRANT ALL ON TABLE "public"."market_news" TO "service_role";



GRANT ALL ON SEQUENCE "public"."market_news_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."market_news_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."market_news_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."market_regime" TO "anon";
GRANT ALL ON TABLE "public"."market_regime" TO "authenticated";
GRANT ALL ON TABLE "public"."market_regime" TO "service_role";



GRANT ALL ON TABLE "public"."master_shortlist" TO "anon";
GRANT ALL ON TABLE "public"."master_shortlist" TO "authenticated";
GRANT ALL ON TABLE "public"."master_shortlist" TO "service_role";



GRANT ALL ON TABLE "public"."ml_training_log" TO "anon";
GRANT ALL ON TABLE "public"."ml_training_log" TO "authenticated";
GRANT ALL ON TABLE "public"."ml_training_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."ml_training_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."ml_training_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."ml_training_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."msl_computed" TO "anon";
GRANT ALL ON TABLE "public"."msl_computed" TO "authenticated";
GRANT ALL ON TABLE "public"."msl_computed" TO "service_role";



GRANT ALL ON TABLE "public"."msl_history" TO "anon";
GRANT ALL ON TABLE "public"."msl_history" TO "authenticated";
GRANT ALL ON TABLE "public"."msl_history" TO "service_role";



GRANT ALL ON TABLE "public"."nifty_total_market" TO "anon";
GRANT ALL ON TABLE "public"."nifty_total_market" TO "authenticated";
GRANT ALL ON TABLE "public"."nifty_total_market" TO "service_role";



GRANT ALL ON TABLE "public"."nifty_upcoming_events" TO "anon";
GRANT ALL ON TABLE "public"."nifty_upcoming_events" TO "authenticated";
GRANT ALL ON TABLE "public"."nifty_upcoming_events" TO "service_role";



GRANT ALL ON SEQUENCE "public"."nifty_upcoming_events_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."nifty_upcoming_events_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nifty_upcoming_events_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."nse_holidays" TO "anon";
GRANT ALL ON TABLE "public"."nse_holidays" TO "authenticated";
GRANT ALL ON TABLE "public"."nse_holidays" TO "service_role";



GRANT ALL ON TABLE "public"."open_positions" TO "anon";
GRANT ALL ON TABLE "public"."open_positions" TO "authenticated";
GRANT ALL ON TABLE "public"."open_positions" TO "service_role";



GRANT ALL ON TABLE "public"."order_history" TO "anon";
GRANT ALL ON TABLE "public"."order_history" TO "authenticated";
GRANT ALL ON TABLE "public"."order_history" TO "service_role";



GRANT ALL ON SEQUENCE "public"."order_history_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."order_history_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."order_history_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."performance_metrics" TO "anon";
GRANT ALL ON TABLE "public"."performance_metrics" TO "authenticated";
GRANT ALL ON TABLE "public"."performance_metrics" TO "service_role";



GRANT ALL ON SEQUENCE "public"."performance_metrics_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."performance_metrics_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."performance_metrics_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."price_history_yf" TO "anon";
GRANT ALL ON TABLE "public"."price_history_yf" TO "authenticated";
GRANT ALL ON TABLE "public"."price_history_yf" TO "service_role";



GRANT ALL ON TABLE "public"."raw_prices" TO "anon";
GRANT ALL ON TABLE "public"."raw_prices" TO "authenticated";
GRANT ALL ON TABLE "public"."raw_prices" TO "service_role";



GRANT ALL ON TABLE "public"."regime_history" TO "anon";
GRANT ALL ON TABLE "public"."regime_history" TO "authenticated";
GRANT ALL ON TABLE "public"."regime_history" TO "service_role";



GRANT ALL ON TABLE "public"."safety_lists" TO "anon";
GRANT ALL ON TABLE "public"."safety_lists" TO "authenticated";
GRANT ALL ON TABLE "public"."safety_lists" TO "service_role";



GRANT ALL ON TABLE "public"."scanner_signals" TO "anon";
GRANT ALL ON TABLE "public"."scanner_signals" TO "authenticated";
GRANT ALL ON TABLE "public"."scanner_signals" TO "service_role";



GRANT ALL ON SEQUENCE "public"."scanner_signals_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."scanner_signals_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."scanner_signals_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."screener_performance" TO "anon";
GRANT ALL ON TABLE "public"."screener_performance" TO "authenticated";
GRANT ALL ON TABLE "public"."screener_performance" TO "service_role";



GRANT ALL ON SEQUENCE "public"."screener_performance_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."screener_performance_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."screener_performance_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."script_change_log" TO "anon";
GRANT ALL ON TABLE "public"."script_change_log" TO "authenticated";
GRANT ALL ON TABLE "public"."script_change_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."script_change_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."script_change_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."script_change_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."sector_strength" TO "anon";
GRANT ALL ON TABLE "public"."sector_strength" TO "authenticated";
GRANT ALL ON TABLE "public"."sector_strength" TO "service_role";



GRANT ALL ON TABLE "public"."shadow_trades" TO "anon";
GRANT ALL ON TABLE "public"."shadow_trades" TO "authenticated";
GRANT ALL ON TABLE "public"."shadow_trades" TO "service_role";



GRANT ALL ON SEQUENCE "public"."shadow_trades_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."shadow_trades_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."shadow_trades_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."signal_daily_summary" TO "anon";
GRANT ALL ON TABLE "public"."signal_daily_summary" TO "authenticated";
GRANT ALL ON TABLE "public"."signal_daily_summary" TO "service_role";



GRANT ALL ON TABLE "public"."signal_log" TO "anon";
GRANT ALL ON TABLE "public"."signal_log" TO "authenticated";
GRANT ALL ON TABLE "public"."signal_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."signal_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."signal_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."signal_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."signal_outcomes" TO "anon";
GRANT ALL ON TABLE "public"."signal_outcomes" TO "authenticated";
GRANT ALL ON TABLE "public"."signal_outcomes" TO "service_role";



GRANT ALL ON SEQUENCE "public"."signal_outcomes_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."signal_outcomes_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."signal_outcomes_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."stock_data_daily" TO "anon";
GRANT ALL ON TABLE "public"."stock_data_daily" TO "authenticated";
GRANT ALL ON TABLE "public"."stock_data_daily" TO "service_role";



GRANT ALL ON TABLE "public"."strategy_config" TO "anon";
GRANT ALL ON TABLE "public"."strategy_config" TO "authenticated";
GRANT ALL ON TABLE "public"."strategy_config" TO "service_role";



GRANT ALL ON TABLE "public"."system_config" TO "anon";
GRANT ALL ON TABLE "public"."system_config" TO "authenticated";
GRANT ALL ON TABLE "public"."system_config" TO "service_role";



GRANT ALL ON TABLE "public"."v_blocked_candidates" TO "anon";
GRANT ALL ON TABLE "public"."v_blocked_candidates" TO "authenticated";
GRANT ALL ON TABLE "public"."v_blocked_candidates" TO "service_role";



GRANT ALL ON TABLE "public"."v_emerging_stocks" TO "anon";
GRANT ALL ON TABLE "public"."v_emerging_stocks" TO "authenticated";
GRANT ALL ON TABLE "public"."v_emerging_stocks" TO "service_role";



GRANT ALL ON TABLE "public"."v_live_position_context" TO "anon";
GRANT ALL ON TABLE "public"."v_live_position_context" TO "authenticated";
GRANT ALL ON TABLE "public"."v_live_position_context" TO "service_role";



GRANT ALL ON TABLE "public"."v_signal_daily_dashboard" TO "anon";
GRANT ALL ON TABLE "public"."v_signal_daily_dashboard" TO "authenticated";
GRANT ALL ON TABLE "public"."v_signal_daily_dashboard" TO "service_role";



GRANT ALL ON TABLE "public"."v_signal_performance" TO "anon";
GRANT ALL ON TABLE "public"."v_signal_performance" TO "authenticated";
GRANT ALL ON TABLE "public"."v_signal_performance" TO "service_role";









ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";



































