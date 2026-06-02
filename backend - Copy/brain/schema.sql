-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Brain Engine v2 Complete Schema
-- Run ONCE in Supabase SQL editor. All tables, indexes, triggers, and seed data.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. brain_proposals ──────────────────────────────────────────────────────
create table if not exists public.brain_proposals (
    id                  bigserial primary key,
    analysis_run_id     text        not null,
    proposal_type       text        not null,
    -- THRESHOLD_CHANGE | ENGINE_WEIGHT | REGIME_WEIGHT | SCRIPT_PATCH | CODE_SUGGESTION | INSIGHT
    target_key          text,           -- system_config key OR script path for SCRIPT_PATCH
    current_value       text,
    proposed_value      text,
    rationale           text        not null,
    evidence            jsonb,          -- {sample_size, win_rate_before, win_rate_after, p_value, fields_analyzed}
    backtest_result     jsonb,          -- {signals_before, signals_after, wr_delta, avg_ret_delta}
    confidence          numeric     check (confidence between 0 and 1),
    status              text        not null default 'PENDING',
    -- PENDING|APPROVED|REJECTED|APPLIED|ROLLED_BACK|EXPIRED|TESTING
    source              text        not null default 'hybrid',   -- quant|llm|hybrid
    priority            integer     default 5,
    high_impact         boolean     default false,
    created_at          timestamptz not null default now(),
    expires_at          timestamptz default (now() + interval '30 days'),
    reviewed_at         timestamptz,
    reviewed_by         text,
    applied_at          timestamptz,
    rollback_value      text,
    script_diff         text,           -- unified diff for SCRIPT_PATCH proposals
    script_backup_path  text,           -- path to backed-up original for rollback
    test_run_id         text            -- brain run ID that validated this change post-apply
);

create index if not exists idx_brain_proposals_status  on public.brain_proposals(status);
create index if not exists idx_brain_proposals_run     on public.brain_proposals(analysis_run_id);
create index if not exists idx_brain_proposals_target  on public.brain_proposals(target_key);
create index if not exists idx_brain_proposals_created on public.brain_proposals(created_at desc);

-- ── 2. config_change_log ────────────────────────────────────────────────────
create table if not exists public.config_change_log (
    id              bigserial   primary key,
    key             text        not null,
    old_value       text,
    new_value       text,
    changed_by      text        not null default 'brain_engine',
    -- brain_engine | manual | pipeline | dashboard_manual | auto_apply | rollback
    proposal_id     bigint      references public.brain_proposals(id) on delete set null,
    reason          text,
    changed_at      timestamptz not null default now(),
    rolled_back_at  timestamptz,
    rolled_back_by  text
);

create index if not exists idx_ccl_key  on public.config_change_log(key);
create index if not exists idx_ccl_date on public.config_change_log(changed_at desc);
create index if not exists idx_ccl_pid  on public.config_change_log(proposal_id);

-- ── 3. script_change_log ────────────────────────────────────────────────────
-- Full versioned history of every script modification the brain makes.
create table if not exists public.script_change_log (
    id              bigserial   primary key,
    proposal_id     bigint      references public.brain_proposals(id) on delete set null,
    script_path     text        not null,   -- relative path e.g. "compute/screen_stocks.py"
    change_type     text        not null,   -- HARDCODE_TO_CONFIG | LOGIC_PATCH | NEW_FIELD | GATE_CHANGE
    diff_text       text        not null,   -- unified diff (git diff format)
    backup_content  text        not null,   -- full original file content for rollback
    git_commit_hash text,                   -- populated after git commit
    applied_at      timestamptz not null default now(),
    applied_by      text        not null default 'brain_engine',
    rolled_back_at  timestamptz,
    rolled_back_by  text,
    rollback_commit text,
    test_result     jsonb,                  -- post-apply test outcomes
    notes           text
);

create index if not exists idx_scl_script  on public.script_change_log(script_path);
create index if not exists idx_scl_date    on public.script_change_log(applied_at desc);
create index if not exists idx_scl_pid     on public.script_change_log(proposal_id);

-- ── 4. brain_analysis_log ───────────────────────────────────────────────────
create table if not exists public.brain_analysis_log (
    id                      bigserial   primary key,
    run_id                  text        not null unique,
    run_date                date        not null,
    period_days             integer     not null,
    run_mode                text        default 'full',  -- full|quant|dry
    signals_analyzed        integer     default 0,
    signals_with_outcomes   integer     default 0,
    coverage_pct            numeric,
    tables_loaded           jsonb,      -- {table_name: {rows, columns, fields_used}}
    scripts_analyzed        jsonb,      -- {script_path: {hardcoded_values_found, tunable_detected}}
    quant_findings          jsonb,
    llm_insights            text,
    proposals_generated     integer     default 0,
    proposals_auto_applied  integer     default 0,
    script_patches_applied  integer     default 0,
    execution_time_sec      numeric,
    errors                  jsonb,
    created_at              timestamptz not null default now()
);

-- ── 5. performance_metrics ──────────────────────────────────────────────────
-- Pre-aggregated performance at daily/weekly/monthly grain.
-- Written by performance_tracker.py after each pipeline run.
create table if not exists public.performance_metrics (
    id              bigserial   primary key,
    metric_date     date        not null,
    grain           text        not null,   -- daily|weekly|monthly
    -- Signal quality
    signals_generated       integer,
    signals_prime           integer,
    signals_breakout        integer,
    signals_staged          integer,
    signals_reentry         integer,
    win_rate_overall        numeric,
    win_rate_prime          numeric,
    win_rate_breakout       numeric,
    win_rate_staged         numeric,
    win_rate_reentry        numeric,
    avg_fwd_ret_5d          numeric,
    avg_fwd_ret_10d         numeric,
    avg_fwd_ret_20d         numeric,
    -- Engine performance (top 3 by win rate stored as jsonb)
    engine_stats            jsonb,   -- {CTL:{wr,n}, MOM:{wr,n}, ...all 10 engines}
    -- Score predictiveness
    score_return_corr       numeric, -- Pearson r of score_adjusted vs max_fwd_return
    holding_score_corr      numeric,
    breakout_readiness_corr numeric,
    -- Regime accuracy
    regime_stats            jsonb,   -- {RISK_ON:{wr,n}, NEUTRAL:{wr,n}, ...}
    -- AI accuracy
    ai_high_conv_wr         numeric, -- win rate when AI said HIGH
    ai_medium_conv_wr       numeric,
    ai_low_conv_wr          numeric,
    -- Brain impact (populated from brain_analysis_log)
    brain_proposals_applied integer default 0,
    brain_wr_delta_avg      numeric,  -- avg win-rate improvement from applied proposals
    -- Regime context at snapshot time
    market_regime           text,
    nifty_5d_ret            numeric,
    india_vix_avg           numeric,
    fii_net_5d              numeric,
    -- Coverage
    outcome_coverage_pct    numeric,
    created_at              timestamptz not null default now(),
    constraint perf_metrics_pkey primary key (metric_date, grain)
);

create index if not exists idx_pm_date  on public.performance_metrics(metric_date desc);
create index if not exists idx_pm_grain on public.performance_metrics(grain, metric_date desc);

-- ── 6. brain_script_registry ────────────────────────────────────────────────
-- Auto-populated by brain on first run. Maps every script to its capabilities.
-- Updated whenever scripts are scanned. Single source of truth for plug-and-play.
create table if not exists public.brain_script_registry (
    id              bigserial   primary key,
    script_path     text        not null unique,   -- relative path from backend/
    module_name     text,                          -- e.g. "compute.screen_stocks"
    purpose         text,                          -- auto-extracted from docstring
    tables_read     text[],                        -- Supabase tables this script reads
    tables_written  text[],                        -- Supabase tables this script writes
    config_keys_used text[],                       -- system_config keys this script reads
    hardcoded_values jsonb,                        -- {line_num: {value, context, tunable}}
    brain_coverage  text default 'PARTIAL',        -- NONE|PARTIAL|FULL
    last_scanned    timestamptz,
    last_modified   timestamptz,
    notes           text,
    created_at      timestamptz not null default now()
);

-- ── 7. open_positions ───────────────────────────────────────────────────────
-- Managed via Telegram buttons + dashboard. Brain reads this for context.
create table if not exists public.open_positions (
    id              bigserial   primary key,
    entry_date      date        not null,
    symbol          text        not null,
    company_name    text,
    sector          text,
    strategy        text,           -- PRIME_SETUP|BREAKOUT|STAGED|REENTRY
    signal_id       bigint      references public.signal_log(id) on delete set null,
    entry_price     numeric     not null,
    entry_price_actual numeric,     -- updated if actual differs from signal price
    quantity        integer,
    stop_loss       numeric,
    target_price    numeric,
    position_size_pct numeric,
    regime_at_entry text,
    ai_conviction   text,
    notes           text,
    telegram_msg_id text,           -- Telegram message ID for button updates
    status          text        not null default 'OPEN', -- OPEN|PARTIAL_EXIT|WATCHING
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint op_pkey primary key (id)
);

create trigger trg_open_positions_updated_at before update on open_positions
    for each row execute function set_updated_at();

-- ── 8. closed_positions ─────────────────────────────────────────────────────
create table if not exists public.closed_positions (
    id              bigserial   primary key,
    open_position_id bigint     references public.open_positions(id) on delete set null,
    symbol          text        not null,
    company_name    text,
    sector          text,
    strategy        text,
    signal_id       bigint      references public.signal_log(id) on delete set null,
    entry_date      date        not null,
    exit_date       date        not null,
    entry_price     numeric     not null,
    exit_price      numeric     not null,
    quantity        integer,
    pnl_pct         numeric     generated always as (
                        round(((exit_price - entry_price) / entry_price) * 100, 2)
                    ) stored,
    pnl_abs         numeric     generated always as (
                        round((exit_price - entry_price) * quantity, 2)
                    ) stored,
    holding_days    integer     generated always as (
                        (exit_date - entry_date)
                    ) stored,
    exit_reason     text,       -- TARGET_HIT|STOP_HIT|MANUAL|REGIME_CHANGE|SIGNAL_DEGRADED
    regime_at_entry text,
    regime_at_exit  text,
    ai_conviction   text,
    notes           text,
    created_at      timestamptz not null default now()
);

create index if not exists idx_cp_symbol on public.closed_positions(symbol);
create index if not exists idx_cp_date   on public.closed_positions(exit_date desc);

-- ── 9. system_config new keys ───────────────────────────────────────────────
insert into public.system_config (key, value, description) values
  ('regime_engine_weights',
   '{"TRENDING":{"CTL":1.2,"MOM":1.2,"SBS":1.1,"VBD":1.0,"IAD":1.0,"RSB":1.0,"TPO":1.0,"RVS":0.7,"SEC":1.0,"EAP":1.0},"RISK ON":{"CTL":1.1,"MOM":1.1,"SBS":1.1,"VBD":1.0,"IAD":1.0,"RSB":1.1,"TPO":1.0,"RVS":0.9,"SEC":1.1,"EAP":1.0},"NEUTRAL":{"CTL":1.0,"MOM":1.0,"SBS":1.0,"VBD":1.0,"IAD":1.0,"RSB":1.0,"TPO":1.0,"RVS":1.0,"SEC":1.0,"EAP":1.0},"RECOVERING":{"CTL":0.8,"MOM":0.7,"SBS":0.9,"VBD":0.8,"IAD":1.3,"RSB":1.3,"TPO":0.9,"RVS":1.5,"SEC":1.2,"EAP":0.8},"RISK OFF":{"CTL":0.6,"MOM":0.5,"SBS":0.7,"VBD":0.6,"IAD":1.2,"RSB":1.2,"TPO":0.7,"RVS":1.4,"SEC":1.0,"EAP":0.6}}',
   'Per-regime engine score multipliers for screen_stocks. Brain-tunable.'),
  ('msl_holding_score_weights',
   '{"momentum":0.30,"structure":0.25,"volume":0.20,"risk":0.15,"fundamental":0.10}',
   'compute_msl holding_score composition weights. Brain-tunable.'),
  ('msl_final_score_weights',
   '{"holding_score":0.40,"validity_score":0.25,"breakout_readiness":0.20,"risk_score_inv":0.15}',
   'compute_msl final_score composition weights. Brain-tunable.'),
  ('screener_convergence_bonus',
   '{"min_engines":3,"bonus_per_engine":0.05,"max_bonus":0.25}',
   'screen_stocks multi-engine convergence bonus config. Brain-tunable.'),
  ('lesson_confidence_decay_rate', '0.95',
   'post_trade: lesson confidence decays by this factor per cycle if not confirmed'),
  ('lesson_min_applications', '5',
   'post_trade: min times a lesson must be applied before it gains full weight'),
  -- Brain engine config
  ('brain_auto_apply_confidence',  '0.90', 'Min confidence for auto-apply (no human needed)'),
  ('brain_auto_apply_backtest_min','5.0',  'Min win-rate improvement (pp) for auto-apply'),
  ('brain_max_proposals_per_run',  '15',   'Cap on proposals per brain cycle'),
  ('brain_period_days',            '90',   'Lookback window in days for brain analysis'),
  ('brain_enabled',                'true', 'Master switch for brain engine'),
  ('brain_script_patching_enabled','false','Allow brain to patch Python scripts (enable after testing)'),
  ('brain_script_auto_patch',      'false','Auto-apply script patches (always false initially)'),
  ('brain_git_commit_patches',     'true', 'Commit script patches to git automatically'),
  -- Performance measurement
  ('perf_win_threshold_pct',       '3.0',  'Forward return % to classify a signal as WIN'),
  ('perf_stop_threshold_pct',      '5.0',  'Forward drawdown % to classify as LOSS'),
  ('perf_eval_horizons',           '[5,10,15,20]', 'Trading day horizons for forward return eval')
on conflict (key) do nothing;

-- ── Trigger for open_positions updated_at ──
-- Only if set_updated_at function exists (it should, you have it for other tables)
-- If not, create it:
create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end; $$;
