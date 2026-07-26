-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 001: sector/industry strength v2
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
--   The v1 function ranked sectors purely on RSI *level*:
--       0.6 * norm(avg_rsi_weekly) + 0.4 * norm(avg_rsi_monthly)
--
--   Three problems for a 1–3 week swing system:
--
--   1. LEVEL, NOT CHANGE. A sector that has been strong for six months
--      outranks one that turned up last week. For a 1–3 week hold the second
--      is the better trade — you want the sector at the start of its move,
--      not the end. v1 structurally cannot see rotation.
--
--   2. NO SIZE FLOOR. `telecom` held rank #1 on four of the last five
--      snapshots with stock_count = 3. Three stocks set the rank that gates
--      the entire CTL/SBS/MOM/RSB universe (max_sector_rank <= 4/7/10).
--
--   3. NO BREADTH OR RETURN. breadth_sma50 was computed and stored but never
--      entered the score. Neither did returns. A sector could rank #1 on RSI
--      while 40% of its members sat below their 50DMA.
--
--   Separately, `rank()` produced gaps on ties (1,1,3) which made
--   `sector_rank <= 4` admit a different number of sectors day to day.
--   v2 uses dense_rank().
--
-- WHAT v2 SCORES  (each component normalised 0–1 across sectors that day)
--
--   trend_level  30%   0.6*norm(rsi_weekly) + 0.4*norm(rsi_monthly)
--   breadth      25%   norm(breadth_sma50)          — is participation broad?
--   rotation     25%   0.5*norm(Δrsi_daily_5d) + 0.5*norm(Δbreadth_5d)
--                      — THE NEW SIGNAL. Is this sector turning up right now?
--   momentum     20%   0.6*norm(avg_ret_1m) + 0.4*norm(avg_rs_vs_nifty)
--
--   Sectors with stock_count < p_min_stocks are still written (so no data is
--   lost) but are ranked AFTER every qualifying sector and flagged
--   qualified = false. Downstream `sector_rank <= N` gates therefore exclude
--   them automatically without any Python change.
--
-- CALLER CONTRACT — IMPORTANT
--   This function reads stock_data_daily WHERE date = p_date and needs
--   sector, market_cap, rsi_*, ret_1m, rs_vs_nifty, above_sma50 to be
--   populated. Those columns are written by compute_indicators.
--   It MUST therefore run AFTER compute_indicators, not before.
--   run_pipeline.py Phase 2 ran it at step 06 (before compute_indicators at
--   step 10), so the aggregate matched zero rows and NOTHING was written —
--   which is why sector_strength was last populated 2026-07-02 while
--   stock_data_daily was current to 2026-07-24. See migration note in
--   run_pipeline.py.
--
-- IDEMPOTENT. Safe to re-run for any date.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. New columns (additive only) ─────────────────────────────────────────
ALTER TABLE public.sector_strength
    ADD COLUMN IF NOT EXISTS composite_score   numeric,
    ADD COLUMN IF NOT EXISTS trend_level_score numeric,
    ADD COLUMN IF NOT EXISTS breadth_score     numeric,
    ADD COLUMN IF NOT EXISTS rotation_score    numeric,
    ADD COLUMN IF NOT EXISTS momentum_score    numeric,
    ADD COLUMN IF NOT EXISTS avg_ret_1m        numeric,
    ADD COLUMN IF NOT EXISTS avg_rs_vs_nifty   numeric,
    ADD COLUMN IF NOT EXISTS d_rsi_daily_5d    numeric,
    ADD COLUMN IF NOT EXISTS d_breadth_5d      numeric,
    ADD COLUMN IF NOT EXISTS rank_5d_ago       integer,
    ADD COLUMN IF NOT EXISTS rank_delta_5d     integer,
    ADD COLUMN IF NOT EXISTS qualified         boolean DEFAULT true,
    ADD COLUMN IF NOT EXISTS computed_at       timestamptz DEFAULT now();

ALTER TABLE public.industry_strength
    ADD COLUMN IF NOT EXISTS composite_score   numeric,
    ADD COLUMN IF NOT EXISTS trend_level_score numeric,
    ADD COLUMN IF NOT EXISTS breadth_score     numeric,
    ADD COLUMN IF NOT EXISTS rotation_score    numeric,
    ADD COLUMN IF NOT EXISTS momentum_score    numeric,
    ADD COLUMN IF NOT EXISTS avg_ret_1m        numeric,
    ADD COLUMN IF NOT EXISTS avg_rs_vs_nifty   numeric,
    ADD COLUMN IF NOT EXISTS d_rsi_daily_5d    numeric,
    ADD COLUMN IF NOT EXISTS d_breadth_5d      numeric,
    ADD COLUMN IF NOT EXISTS rank_5d_ago       integer,
    ADD COLUMN IF NOT EXISTS rank_delta_5d     integer,
    ADD COLUMN IF NOT EXISTS qualified         boolean DEFAULT true,
    ADD COLUMN IF NOT EXISTS computed_at       timestamptz DEFAULT now();

COMMENT ON COLUMN public.sector_strength.rotation_score IS
    'Normalised 5-session change in avg_rsi_daily and breadth_sma50. This is the '
    'component v1 lacked entirely — it detects a sector turning up, which is what '
    'a 1-3 week swing entry needs, as opposed to one that is already extended.';
COMMENT ON COLUMN public.sector_strength.qualified IS
    'false when stock_count < min_stocks threshold. Unqualified sectors are ranked '
    'after all qualified ones so that downstream `sector_rank <= N` gates exclude '
    'them without any application-code change.';


-- ── 2. Shared scoring CTE, applied to sectors then industries ──────────────
CREATE OR REPLACE FUNCTION public.fn_compute_sector_industry_strength(
    p_date       date    DEFAULT CURRENT_DATE,
    p_min_stocks integer DEFAULT 5,
    p_lookback   integer DEFAULT 5
) RETURNS TABLE(scope text, rows_written integer)
    LANGUAGE plpgsql
    AS $$
declare
  v_prev_date  date;
  v_sec_rows   integer := 0;
  v_ind_rows   integer := 0;
begin
  -- Resolve the trading day p_lookback sessions before p_date, using dates
  -- that actually carry computed indicator data (rsi_daily not null), so a
  -- half-written bhavcopy row can never be picked as the comparison anchor.
  select d into v_prev_date
  from (
      select distinct date as d
      from stock_data_daily
      where date < p_date and rsi_daily is not null
      order by date desc
      limit p_lookback
  ) t
  order by d asc
  limit 1;

  -- ══ SECTORS ═════════════════════════════════════════════════════════════
  with cur as (
    select
      sector,
      count(*)::int                          as stock_count,
      avg(rsi_daily)                         as avg_rsi_daily,
      avg(rsi_weekly)                        as avg_rsi_weekly,
      avg(rsi_monthly)                       as avg_rsi_monthly,
      avg(ret_1m)                            as avg_ret_1m,
      avg(ret_6m)                            as avg_ret_6m,
      avg(rs_vs_nifty)                       as avg_rs_vs_nifty,
      100.0 * count(*) filter (where above_sma50)
            / nullif(count(*) filter (where above_sma50 is not null), 0) as breadth_sma50
    from stock_data_daily
    where date = p_date
      and sector is not null
      and sector not in ('', 'indices')
      and coalesce(market_cap, 0) > 0
    group by sector
  ),
  prev as (
    select
      sector,
      avg(rsi_daily) as avg_rsi_daily,
      100.0 * count(*) filter (where above_sma50)
            / nullif(count(*) filter (where above_sma50 is not null), 0) as breadth_sma50
    from stock_data_daily
    where date = v_prev_date
      and sector is not null
      and sector not in ('', 'indices')
      and coalesce(market_cap, 0) > 0
    group by sector
  ),
  delta as (
    select
      c.*,
      c.avg_rsi_daily  - p.avg_rsi_daily  as d_rsi_daily_5d,
      c.breadth_sma50  - p.breadth_sma50  as d_breadth_5d
    from cur c left join prev p using (sector)
  ),
  -- Normalisation bounds are computed over QUALIFYING sectors only, so a
  -- 2-stock outlier cannot stretch the scale and flatten everyone else.
  b as (
    select
      min(avg_rsi_weekly)  mn_w,  max(avg_rsi_weekly)  mx_w,
      min(avg_rsi_monthly) mn_m,  max(avg_rsi_monthly) mx_m,
      min(breadth_sma50)   mn_b,  max(breadth_sma50)   mx_b,
      min(d_rsi_daily_5d)  mn_dr, max(d_rsi_daily_5d)  mx_dr,
      min(d_breadth_5d)    mn_db, max(d_breadth_5d)    mx_db,
      min(avg_ret_1m)      mn_r1, max(avg_ret_1m)      mx_r1,
      min(avg_rs_vs_nifty) mn_rs, max(avg_rs_vs_nifty) mx_rs
    from delta
    where stock_count >= p_min_stocks
  ),
  scored as (
    select d.*,
      -- norm() helper inlined: (x-min)/(max-min), 0.5 when the range is
      -- degenerate (all equal / single sector / all null).
      -- Clamped to [0,1]: bounds come from QUALIFYING sectors only, so a
      -- sub-floor sector can land outside the range (a 2-stock sector scored
      -- rotation 1.777 in validation). It is ranked last regardless, but an
      -- out-of-range value stored in the table would misread on the dashboard.
      greatest(0, least(1, coalesce((d.avg_rsi_weekly  - b.mn_w ) / nullif(b.mx_w  - b.mn_w , 0), 0.5))) as n_w,
      greatest(0, least(1, coalesce((d.avg_rsi_monthly - b.mn_m ) / nullif(b.mx_m  - b.mn_m , 0), 0.5))) as n_m,
      greatest(0, least(1, coalesce((d.breadth_sma50   - b.mn_b ) / nullif(b.mx_b  - b.mn_b , 0), 0.5))) as n_b,
      greatest(0, least(1, coalesce((d.d_rsi_daily_5d  - b.mn_dr) / nullif(b.mx_dr - b.mn_dr, 0), 0.5))) as n_dr,
      greatest(0, least(1, coalesce((d.d_breadth_5d    - b.mn_db) / nullif(b.mx_db - b.mn_db, 0), 0.5))) as n_db,
      greatest(0, least(1, coalesce((d.avg_ret_1m      - b.mn_r1) / nullif(b.mx_r1 - b.mn_r1, 0), 0.5))) as n_r1,
      greatest(0, least(1, coalesce((d.avg_rs_vs_nifty - b.mn_rs) / nullif(b.mx_rs - b.mn_rs, 0), 0.5))) as n_rs
    from delta d cross join b
  ),
  composed as (
    select s.*,
      (0.30 * (0.6 * n_w  + 0.4 * n_m ))
    + (0.25 * n_b)
    + (0.25 * (0.5 * n_dr + 0.5 * n_db))
    + (0.20 * (0.6 * n_r1 + 0.4 * n_rs))            as composite_score,
      (0.6 * n_w + 0.4 * n_m)                       as trend_level_score,
      n_b                                           as breadth_score,
      (0.5 * n_dr + 0.5 * n_db)                     as rotation_score,
      (0.6 * n_r1 + 0.4 * n_rs)                     as momentum_score,
      (s.stock_count >= p_min_stocks)               as qualified
    from scored s
  ),
  ranked as (
    select c.*,
      -- Unqualified sectors sort after every qualified one regardless of score.
      dense_rank() over (
        order by (case when c.qualified then 0 else 1 end),
                 c.composite_score desc
      )::int as composite_rank
    from composed c
  ),
  prev_rank as (
    select sector, rank as r from sector_strength where date = v_prev_date
  ),
  final as (
    select r.*, pr.r as rank_5d_ago, (pr.r - r.composite_rank)::int as rank_delta_5d
    from ranked r left join prev_rank pr using (sector)
  )
  insert into sector_strength
    (date, sector, stock_count, avg_rsi_daily, avg_rsi_weekly, avg_rsi_monthly,
     avg_ret_6m, breadth_sma50, rank, top4_flag, sector_state,
     composite_score, trend_level_score, breadth_score, rotation_score,
     momentum_score, avg_ret_1m, avg_rs_vs_nifty, d_rsi_daily_5d, d_breadth_5d,
     rank_5d_ago, rank_delta_5d, qualified, computed_at)
  select
    p_date, sector, stock_count,
    round(avg_rsi_daily,   5), round(avg_rsi_weekly, 5), round(avg_rsi_monthly, 5),
    round(avg_ret_6m,      5), round(breadth_sma50,  8),
    composite_rank,
    composite_rank <= 5 and qualified,
    -- sector_state now carries rotation information, not just extension.
    case
      when not qualified                                    then 'TOO_SMALL'
      when composite_rank <= 4 and coalesce(avg_ret_6m,0) > 30
                                and coalesce(rotation_score,0.5) < 0.5
                                                            then 'EXTENDED - PULLBACK ONLY'
      when composite_rank <= 5 and coalesce(rank_delta_5d,0) >= 3  then 'LEADING - IMPROVING'
      when composite_rank <= 5                              then 'LEADING'
      when coalesce(rank_delta_5d,0) >= 4                   then 'IMPROVING'
      when coalesce(rank_delta_5d,0) <= -4                  then 'WEAKENING'
      else 'NORMAL'
    end,
    round(composite_score,   6), round(trend_level_score, 6),
    round(breadth_score,     6), round(rotation_score,    6),
    round(momentum_score,    6), round(avg_ret_1m,        5),
    round(avg_rs_vs_nifty,   5), round(d_rsi_daily_5d,    5),
    round(d_breadth_5d,      5),
    rank_5d_ago, rank_delta_5d, qualified, now()
  from final
  on conflict (date, sector) do update set
    stock_count = excluded.stock_count,
    avg_rsi_daily = excluded.avg_rsi_daily,
    avg_rsi_weekly = excluded.avg_rsi_weekly,
    avg_rsi_monthly = excluded.avg_rsi_monthly,
    avg_ret_6m = excluded.avg_ret_6m,
    breadth_sma50 = excluded.breadth_sma50,
    rank = excluded.rank,
    top4_flag = excluded.top4_flag,
    sector_state = excluded.sector_state,
    composite_score = excluded.composite_score,
    trend_level_score = excluded.trend_level_score,
    breadth_score = excluded.breadth_score,
    rotation_score = excluded.rotation_score,
    momentum_score = excluded.momentum_score,
    avg_ret_1m = excluded.avg_ret_1m,
    avg_rs_vs_nifty = excluded.avg_rs_vs_nifty,
    d_rsi_daily_5d = excluded.d_rsi_daily_5d,
    d_breadth_5d = excluded.d_breadth_5d,
    rank_5d_ago = excluded.rank_5d_ago,
    rank_delta_5d = excluded.rank_delta_5d,
    qualified = excluded.qualified,
    computed_at = excluded.computed_at;

  get diagnostics v_sec_rows = row_count;

  -- ══ INDUSTRIES ══════════════════════════════════════════════════════════
  -- Same methodology. Industries are finer-grained (83 vs 23) so the size
  -- floor is proportionally smaller — 3 rather than 5.
  with cur as (
    select
      industry,
      count(*)::int                          as stock_count,
      avg(rsi_daily)                         as avg_rsi_daily,
      avg(rsi_weekly)                        as avg_rsi_weekly,
      avg(rsi_monthly)                       as avg_rsi_monthly,
      avg(ret_1m)                            as avg_ret_1m,
      avg(ret_6m)                            as avg_ret_6m,
      avg(rs_vs_nifty)                       as avg_rs_vs_nifty,
      100.0 * count(*) filter (where above_sma50)
            / nullif(count(*) filter (where above_sma50 is not null), 0) as breadth_sma50
    from stock_data_daily
    where date = p_date
      and industry is not null
      and industry not in ('', 'indices')
      and coalesce(market_cap, 0) > 0
    group by industry
  ),
  prev as (
    select
      industry,
      avg(rsi_daily) as avg_rsi_daily,
      100.0 * count(*) filter (where above_sma50)
            / nullif(count(*) filter (where above_sma50 is not null), 0) as breadth_sma50
    from stock_data_daily
    where date = v_prev_date
      and industry is not null
      and industry not in ('', 'indices')
      and coalesce(market_cap, 0) > 0
    group by industry
  ),
  delta as (
    select c.*,
      c.avg_rsi_daily - p.avg_rsi_daily as d_rsi_daily_5d,
      c.breadth_sma50 - p.breadth_sma50 as d_breadth_5d
    from cur c left join prev p using (industry)
  ),
  b as (
    select
      min(avg_rsi_weekly)  mn_w,  max(avg_rsi_weekly)  mx_w,
      min(avg_rsi_monthly) mn_m,  max(avg_rsi_monthly) mx_m,
      min(breadth_sma50)   mn_b,  max(breadth_sma50)   mx_b,
      min(d_rsi_daily_5d)  mn_dr, max(d_rsi_daily_5d)  mx_dr,
      min(d_breadth_5d)    mn_db, max(d_breadth_5d)    mx_db,
      min(avg_ret_1m)      mn_r1, max(avg_ret_1m)      mx_r1,
      min(avg_rs_vs_nifty) mn_rs, max(avg_rs_vs_nifty) mx_rs
    from delta
    where stock_count >= greatest(p_min_stocks - 2, 3)
  ),
  scored as (
    select d.*,
      greatest(0, least(1, coalesce((d.avg_rsi_weekly  - b.mn_w ) / nullif(b.mx_w  - b.mn_w , 0), 0.5))) as n_w,
      greatest(0, least(1, coalesce((d.avg_rsi_monthly - b.mn_m ) / nullif(b.mx_m  - b.mn_m , 0), 0.5))) as n_m,
      greatest(0, least(1, coalesce((d.breadth_sma50   - b.mn_b ) / nullif(b.mx_b  - b.mn_b , 0), 0.5))) as n_b,
      greatest(0, least(1, coalesce((d.d_rsi_daily_5d  - b.mn_dr) / nullif(b.mx_dr - b.mn_dr, 0), 0.5))) as n_dr,
      greatest(0, least(1, coalesce((d.d_breadth_5d    - b.mn_db) / nullif(b.mx_db - b.mn_db, 0), 0.5))) as n_db,
      greatest(0, least(1, coalesce((d.avg_ret_1m      - b.mn_r1) / nullif(b.mx_r1 - b.mn_r1, 0), 0.5))) as n_r1,
      greatest(0, least(1, coalesce((d.avg_rs_vs_nifty - b.mn_rs) / nullif(b.mx_rs - b.mn_rs, 0), 0.5))) as n_rs
    from delta d cross join b
  ),
  composed as (
    select s.*,
      (0.30 * (0.6 * n_w  + 0.4 * n_m ))
    + (0.25 * n_b)
    + (0.25 * (0.5 * n_dr + 0.5 * n_db))
    + (0.20 * (0.6 * n_r1 + 0.4 * n_rs))       as composite_score,
      (0.6 * n_w + 0.4 * n_m)                  as trend_level_score,
      n_b                                      as breadth_score,
      (0.5 * n_dr + 0.5 * n_db)                as rotation_score,
      (0.6 * n_r1 + 0.4 * n_rs)                as momentum_score,
      (s.stock_count >= greatest(p_min_stocks - 2, 3)) as qualified
    from scored s
  ),
  ranked as (
    select c.*,
      dense_rank() over (
        order by (case when c.qualified then 0 else 1 end), c.composite_score desc
      )::int as composite_rank
    from composed c
  ),
  prev_rank as (
    select industry, rank as r from industry_strength where date = v_prev_date
  ),
  final as (
    select r.*, pr.r as rank_5d_ago, (pr.r - r.composite_rank)::int as rank_delta_5d
    from ranked r left join prev_rank pr using (industry)
  )
  insert into industry_strength
    (date, industry, stock_count, avg_rsi_daily, avg_rsi_weekly, avg_rsi_monthly,
     avg_ret_6m, breadth_sma50, rank, top5_flag, industry_state,
     composite_score, trend_level_score, breadth_score, rotation_score,
     momentum_score, avg_ret_1m, avg_rs_vs_nifty, d_rsi_daily_5d, d_breadth_5d,
     rank_5d_ago, rank_delta_5d, qualified, computed_at)
  select
    p_date, industry, stock_count,
    round(avg_rsi_daily,   5), round(avg_rsi_weekly, 5), round(avg_rsi_monthly, 5),
    round(avg_ret_6m,      5), round(breadth_sma50,  8),
    composite_rank,
    composite_rank <= 5 and qualified,
    case
      when not qualified                                    then 'TOO_SMALL'
      when composite_rank <= 4 and coalesce(avg_ret_6m,0) > 30
                                and coalesce(rotation_score,0.5) < 0.5
                                                            then 'EXTENDED - PULLBACK ONLY'
      when composite_rank <= 5 and coalesce(rank_delta_5d,0) >= 3  then 'LEADING - IMPROVING'
      when composite_rank <= 5                              then 'LEADING'
      when coalesce(rank_delta_5d,0) >= 4                   then 'IMPROVING'
      when coalesce(rank_delta_5d,0) <= -4                  then 'WEAKENING'
      else 'NORMAL'
    end,
    round(composite_score,   6), round(trend_level_score, 6),
    round(breadth_score,     6), round(rotation_score,    6),
    round(momentum_score,    6), round(avg_ret_1m,        5),
    round(avg_rs_vs_nifty,   5), round(d_rsi_daily_5d,    5),
    round(d_breadth_5d,      5),
    rank_5d_ago, rank_delta_5d, qualified, now()
  from final
  on conflict (date, industry) do update set
    stock_count = excluded.stock_count,
    avg_rsi_daily = excluded.avg_rsi_daily,
    avg_rsi_weekly = excluded.avg_rsi_weekly,
    avg_rsi_monthly = excluded.avg_rsi_monthly,
    avg_ret_6m = excluded.avg_ret_6m,
    breadth_sma50 = excluded.breadth_sma50,
    rank = excluded.rank,
    top5_flag = excluded.top5_flag,
    industry_state = excluded.industry_state,
    composite_score = excluded.composite_score,
    trend_level_score = excluded.trend_level_score,
    breadth_score = excluded.breadth_score,
    rotation_score = excluded.rotation_score,
    momentum_score = excluded.momentum_score,
    avg_ret_1m = excluded.avg_ret_1m,
    avg_rs_vs_nifty = excluded.avg_rs_vs_nifty,
    d_rsi_daily_5d = excluded.d_rsi_daily_5d,
    d_breadth_5d = excluded.d_breadth_5d,
    rank_5d_ago = excluded.rank_5d_ago,
    rank_delta_5d = excluded.rank_delta_5d,
    qualified = excluded.qualified,
    computed_at = excluded.computed_at;

  get diagnostics v_ind_rows = row_count;

  -- Returning counts (rather than void) lets the Python caller assert that
  -- rows were actually written. The v1 signature returned void, so the caller
  -- logged "✓ sector/industry strength computed" even when it wrote nothing —
  -- which is precisely how this went unnoticed for 22 days.
  return query select 'sector'::text, v_sec_rows
               union all
               select 'industry'::text, v_ind_rows;
end;
$$;

ALTER FUNCTION public.fn_compute_sector_industry_strength(date, integer, integer)
    OWNER TO postgres;

GRANT ALL ON FUNCTION public.fn_compute_sector_industry_strength(date, integer, integer) TO anon;
GRANT ALL ON FUNCTION public.fn_compute_sector_industry_strength(date, integer, integer) TO authenticated;
GRANT ALL ON FUNCTION public.fn_compute_sector_industry_strength(date, integer, integer) TO service_role;

-- ── 3. Drop the old void-returning overload ────────────────────────────────
-- Postgres allows both to coexist because the signatures differ, and
-- sb.rpc("fn_compute_sector_industry_strength", {"p_date": ...}) would then be
-- ambiguous. Remove the v1 signature explicitly.
DROP FUNCTION IF EXISTS public.fn_compute_sector_industry_strength(date);


-- ── 4. Backfill ────────────────────────────────────────────────────────────
-- sector_strength has been empty since 2026-07-02 while stock_data_daily is
-- current. Rebuild every trading day that has indicator data so that
-- rank_delta_5d and the Brain's history queries have a continuous series.
DO $$
DECLARE d date;
BEGIN
  FOR d IN
    SELECT DISTINCT date FROM stock_data_daily
    WHERE rsi_daily IS NOT NULL AND date >= CURRENT_DATE - 120
    ORDER BY date
  LOOP
    PERFORM public.fn_compute_sector_industry_strength(d, 5, 5);
  END LOOP;
END $$;
