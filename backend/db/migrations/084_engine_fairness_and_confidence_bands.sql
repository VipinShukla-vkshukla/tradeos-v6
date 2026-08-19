-- 084 — engine fairness in the allocator queue, confidence-banded priors,
--       a post-F-33 prior floor date, and a faster exit-only guard lane.
--       19-Aug-2026.
--
-- WHY, IN ONE PARAGRAPH
-- ---------------------
-- `edge` is `prior.mean_r - cost_r` and `prior` is keyed on the ENGINE, so
-- every candidate from one engine carries nearly the same edge and a pooled
-- descending sort ranks ENGINES, not setups. Measured 13-19 Aug 2026: 29 of
-- 32 closed intraday positions came from SDN, while ORB wrote 561 TAKEN rows
-- and closed one. Separately, confidence means something different in every
-- engine — terciled WITHIN each engine over every TAKEN-and-resolved row,
-- gross R is INVERTED for SDN/PDL/VWR, noise for ORB at n=1030, and correctly
-- ordered only for VCE — so confidence cannot be ranked on directly, but what
-- confidence has been WORTH per engine can be, and that is a prior key.
--
-- Three of these four ship INERT. Only engine fairness is armed, because it
-- reorders a QUEUE and cannot admit anything that fails the bar.

-- ── 1. ENGINE FAIRNESS — ARMED ──────────────────────────────────────────────
-- Interleaves the allocator's queue so every engine's BEST candidate is
-- considered before any engine's second. The bar is untouched: a proposal
-- below it still declines, so the only outcomes that change are those where a
-- lower-ranked engine's candidate ALREADY cleared the bar and lost its slot to
-- a same-engine sibling. That is the whole defect, and it is why this one is
-- safe to arm rather than shadow.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('alloc_intraday_engine_fairness', 'true',
        'Allocator queues every engine''s best intraday candidate before any '
        'engine''s second, instead of one pooled sort by edge. Because edge is '
        'keyed on the engine prior, the pooled sort ranked engines rather than '
        'setups: 29 of 32 closed positions 13-19 Aug 2026 came from SDN while '
        'ORB wrote 561 TAKEN rows and closed one. Cannot admit a proposal that '
        'fails the bar - it reorders the queue only. false restores the pooled '
        'sort exactly. allocation/policies.py::_interleave_by_engine.',
        'Master controls', 'allocation/policies.py',
        'bool', 'true', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

-- ── 2. CONFIDENCE-BANDED PRIORS — INERT ─────────────────────────────────────
-- Prices a proposal from its engine's record AT ITS OWN CONFIDENCE LEVEL
-- rather than that engine's record pooled across levels it does not treat
-- alike. Ships false: the measured band table is dominated by rows recorded
-- under the PRE-F-33 stop geometry, and arming it on that sample would pin
-- each engine's slope to a strategy the book no longer trades.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('alloc_intraday_confidence_bands', 'false',
        'Segments each intraday engine''s prior by the confidence its own '
        'detector assigned, so a proposal is priced from what THAT engine''s '
        'setups at THAT confidence have historically returned. Measured 19-Aug '
        '2026, gross R by within-engine tercile: VCE -0.599/-0.510/+0.430 '
        '(ordered), SDN +0.725/+0.265/+0.191 (inverted), ORB -0.424/-0.641/'
        '-0.220 (noise at n=1030). INERT BY DEFAULT - the sample is mostly '
        'pre-F-33 stop geometry. Arm only alongside priors_intraday_since. '
        'allocation/scoring.py::confidence_band.',
        'Master controls', 'allocation/scoring.py',
        'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('intraday_prior_confidence_band_edges', '0.65,0.75',
        'Ascending confidence cuts defining the bands used by '
        'alloc_intraday_confidence_bands. "0.65,0.75" gives three bands: C0 '
        '(<0.65), C1 (0.65-0.75), C2 (>=0.75). A malformed value degrades to '
        'NO bands (and logs), never to one giant band that would read like a '
        'measurement. Read by allocation/scoring.py::band_edges.',
        'Master controls', 'allocation/scoring.py',
        'string', '0.65,0.75', 'SAFE')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

-- ── 3. A HARD PRIOR FLOOR DATE — INERT ──────────────────────────────────────
-- priors_intraday_lookback_days is a ROLLING window and cannot express
-- "everything before this date measured different rules". On 18-Aug-2026
-- base.risk_from_structure stopped clamping structural stops (F-33): the
-- clamped population ran -0.5348R against +0.0154R for rows whose stop
-- survived, over 1,766 rows. A prior spanning that date averages two
-- strategies and calls the result one engine's record.
--
-- UNSET DELIBERATELY. Arming it on 19-Aug would drop every engine below
-- priors_min_sample_intraday at once (19 clean rows existed across three
-- engines), landing them all in _cold_start - permissive, per scoring.py's own
-- rule, but blind. Set it to '2026-08-19' once enough post-fix sessions exist.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('priors_intraday_since',
        '',
        'Hard floor date (YYYY-MM-DD) for the intraday prior population, '
        'combined with priors_intraday_lookback_days by taking the LATER of '
        'the two. Empty = no floor, today''s behaviour exactly. Exists because '
        'the F-33 stop-geometry fix (18-Aug-2026) changed what the engines '
        'trade, so rows either side of it are not one population: clamped '
        'stops ran -0.5348R vs +0.0154R structural, n=1766. Set to 2026-08-19 '
        'only once each engine that matters has ~30 post-fix TAKEN rows, or '
        'every prior drops to a cold start at once.',
        'Master controls', 'allocation/scoring.py',
        'string', '', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

-- ── 4. THE EXIT-ONLY FAST LANE — ARMED ──────────────────────────────────────
-- Finding a setup and defending an open position shared one 15s timer sized
-- for the expensive one. Measured 19-Aug: the loop ran a 16.0s median, 28s
-- p99, 44s max - so up to a full interval of adverse movement was absorbed
-- before a breached stop was even proposed. The guard reads self.positions
-- (in memory, 0-4 rows, no DB read) and writes only when a rung actually
-- fires, so raising its cadence does not move the Supabase footprint - which
-- is what ruled out simply lowering intraday_eval_interval_s.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, min_value, max_value, default_value,
                           risk_level)
VALUES ('intraday_position_guard_interval_s', '3',
        'How often OPEN intraday positions are re-checked against the exit '
        'ladder, between full decision cycles. Split out of '
        'intraday_eval_interval_s (15s) on 19-Aug-2026: the full scan is ~120 '
        'symbols x 9 engines and writes detection rows, while the exit check '
        'is in-memory over a handful of positions and writes only when a rung '
        'fires. Entries are NOT affected - the guard never evaluates '
        'candidates or setups. Set >= intraday_eval_interval_s to disable the '
        'fast lane exactly. intraday/engine.py::guard_positions.',
        'Master controls', 'intraday/run.py',
        'int', 1, 60, '3', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

-- ── 5. SDN CONFIDENCE CAP — recorded, already applied 19-Aug-2026 ───────────
-- Written directly to system_config earlier the same day; restated here so a
-- rebuild from migrations reproduces the live book. See F-33 section 5 and
-- intraday/strategies/short_distribution.py.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, min_value, max_value, default_value,
                           risk_level)
VALUES ('intraday_short_max_confidence', '0.75',
        'Refuses an SDN setup whose own detection confidence is >= this value. '
        'SDN''s confidence runs BACKWARDS: full history n=265, the 0.75+ bucket '
        'returned -0.273R with a 63.4%% stop rate against +0.769R for 0.55-0.62; '
        're-checked on 13-19 Aug live trades (n=29), 0.75+ returned -0.229R at a '
        '20%% win rate against +0.261R at 71%% for the lowest bucket. '
        'registry.evaluate_all sorts by -confidence, so the book funded SDN''s '
        'worst detections first. 0 disables. The real repair is the confidence '
        'FORMULA, not this threshold.',
        'Master controls', 'intraday/strategies/short_distribution.py',
        'float', 0, 1, '0.0', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                description = EXCLUDED.description,
                                updated_at = now();
