-- 090_swing_exit_reprice_and_entry_discipline.sql
-- 20-Aug-2026 (F-43)
--
-- Full swing-book review against the last 15 closed trades. Five findings,
-- all evidenced from live data, documented in docs/FINDINGS.md F-43.
-- Every code-side change these keys arm is in the same commit. No
-- intraday-framework file (strategies, exit_policy.py, gates, registry) is
-- touched by this migration or its code.

-- ── 1. EXIT LADDER REPRICED TO WHAT THE BOOK ACTUALLY PRODUCES ─────────────
-- Median peak run across 13 closed swing trades with an MFE figure was
-- 0.67R; only 2 of 13 ever cleared 1.0R; 1.5R was reached once. The old
-- ladder (1.5R partial, 1.0R breakeven, 2.0R trail-on) sat above the
-- distribution, so the give-back guard — built as a loss-prevention
-- backstop — ended up doing 100% of the book's profit-taking at its flat
-- 50% setting: 7 of the last 9 winning exits gave back within 1-6% of
-- exactly half their peak. See control/position_lifecycle.py's
-- load_exit_policy() for the full note.
UPDATE system_config SET value = '1.0', updated_at = NOW()
    WHERE key = 'exit_partial_book_r';
UPDATE system_config SET value = '0.5', updated_at = NOW()
    WHERE key = 'exit_breakeven_at_r';
UPDATE system_config SET value = '1.5', updated_at = NOW()
    WHERE key = 'exit_trail_after_r';
UPDATE system_config SET value = '1.0', updated_at = NOW()
    WHERE key = 'exit_trail_r';
UPDATE system_config SET value = '0.6', updated_at = NOW()
    WHERE key = 'swing_setup_target_min_r';

-- ── 2. GIVE-BACK GUARD TIERED BY PEAK-R ─────────────────────────────────────
-- Below giveback_runner_min_r (now matched to the repriced partial, 1.0R)
-- the guard is the ONLY thing protecting an open winner and stays at the
-- existing 50% — loose on purpose, so ordinary wobble before a trade has
-- earned its partial is not chopped. At or above that line a partial should
-- already be banked and the stop at breakeven, so what remains is risking
-- profit rather than capital and is tightened to 30%.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('exit_giveback_pct_runner', '30.0',
        'Give-back allowance, in percent of peak R, once a swing position''s '
        'peak has cleared exit_giveback_runner_min_r — tighter than the base '
        'exit_giveback_pct because a partial should already be banked and the '
        'stop at breakeven by that point, so this is defending open profit, '
        'not capital. control/position_lifecycle.py::evaluate_exit.',
        'Exit ladder', 'control/position_lifecycle.py', 'float', '30.0', 'LOW'),
       ('exit_giveback_runner_min_r', '1.0',
        'Peak-R line above which the give-back guard switches from '
        'exit_giveback_pct to the tighter exit_giveback_pct_runner. Matches '
        'exit_partial_book_r by construction so the two rules hand off with '
        'no gap in R-space.',
        'Exit ladder', 'control/position_lifecycle.py', 'float', '1.0', 'LOW')
ON CONFLICT (key) DO NOTHING;

-- ── 3. THE EVENING PIPELINE'S OWN REFUSAL, NEVER WIRED ──────────────────────
-- analysis/entry_ranking.py::entry_refusals() has honoured filter_reason
-- since 18-Aug (the GABRIEL fix) but the switch it reads was never created,
-- so it defaulted OFF the whole time. Five of six swing positions open
-- today were entered on plans the pipeline had itself refused the same
-- night (insufficient_rr_0.78x and similar) — a live price move can
-- legitimately clear R:R the close-based pipeline missed, but a plan the
-- pipeline named as refused deserves an explicit decision to override it,
-- not a silently-absent config row.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('entry_respect_filter_reason', 'true',
        'Honour signal_output_daily.filter_reason as a hard refusal '
        '(insufficient_rr_*, blocked_*, rejected_*, veto_*) inside '
        'entry_ranking.entry_refusals() before a swing entry is placed. Was '
        'coded 18-Aug and never armed. See F-43.',
        'Entry gates', 'analysis/entry_ranking.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;

-- ── 4. ABSOLUTE RANK FLOOR — CLOSES THE PATH THAT LET RANK -16 IN ──────────
-- entry_ranking's relative top-N gate is switched off by design whenever
-- alloc_live_swing is true (it is, today) because the allocator's edge is
-- now the live veto — but edge is an opportunity-cost question, not a
-- verdict on the plan's own composite quality, and act_on_candidates()
-- sends every candidate outside the top swing_alert_top_n contenders
-- straight to the order path with NO rank check of any kind. TRAVELFOOD
-- entered 14-Aug at composite rank -16. This floor is unconditional and
-- applies regardless of alloc_live_swing or the top-N gate's own state.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('swing_min_rank_to_enter', '0',
        'A swing plan whose entry_ranking composite score is below this '
        'value is refused at the order choke point (_maybe_enter_swing), '
        'regardless of daily quota room, the top-N relative gate''s state, '
        'or what the allocator scored it. Unconditional absolute floor, '
        'distinct from the relative top-N gate. See F-43 (TRAVELFOOD, rank '
        '-16, 14-Aug-2026).',
        'Entry gates', 'intraday/engine.py', 'float', '0', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;

-- ── 5. SIZING — FEWER, LARGER POSITIONS AT THE SAME TOTAL RISK ─────────────
-- Average swing position was Rs.2,989 against a Rs.6,000 order-value cap —
-- the cap was never binding. risk_pct_per_trade (1% of capital) was: at
-- 5-9%-wide ATR stops, 1% of a Rs.30,000 sleeve produces a smaller share
-- count than a wider stop would take even at the "generous" max_position_pct
-- ceiling. The flat Rs.15.04 DP fee plus STT/stamp on both legs then landed
-- on a small base — charges were 36.6% of gross profit over the last 15
-- closed trades. This does NOT raise total book risk: 4 positions at 1.5%
-- = 6% deployed risk, same as the old 6 positions at 1.0% = 6%, against an
-- unchanged 8% ceiling (portfolio_max_total_risk_pct). It concentrates the
-- SAME risk budget into fewer, more meaningful positions instead of
-- spreading it thin enough that transaction costs dominate the outcome.
-- max_position_pct and swing_max_order_value are raised in lockstep so
-- neither becomes an accidental new binding wall below the real lever
-- (risk_pct_per_trade).
UPDATE system_config SET value = '1.5', updated_at = NOW()
    WHERE key = 'risk_pct_per_trade';
UPDATE system_config SET value = '4', updated_at = NOW()
    WHERE key = 'max_positions_neutral';
UPDATE system_config SET value = '25.0', updated_at = NOW()
    WHERE key = 'max_position_pct';
UPDATE system_config SET value = '8000', updated_at = NOW()
    WHERE key = 'swing_max_order_value';
-- Matches the blueprint's own documented intent (docs/0_SYSTEM_BLUEPRINT.md
-- section 2: "Daily entry cap ... swing_max_new_per_day (2)") — the live
-- config had drifted to 3 with no record of why. Fewer entries per day
-- raises the bar each one has to clear rather than filling a quota.
UPDATE system_config SET value = '2', updated_at = NOW()
    WHERE key = 'swing_max_new_per_day';
