-- 11-Aug-2026, same day migration 066 shipped swing_entry_min_gap_minutes/
-- intraday_entry_min_gap_minutes. Revised after the operator asked, before
-- merge, whether a minimum gap would cost genuine simultaneous
-- opportunities -- checked against the real 10-Aug sequence and the
-- answer was yes: the allocator scored 8 proposals together at 09:36:23
-- ("3 to take, 5 refused") and picked AUBANK and SCI as two of that day's
-- three best trades in ONE joint decision, two seconds apart. A 20-minute
-- gap would have let AUBANK through and refused SCI for 20 minutes for no
-- reason -- overriding a decision the allocator had already made well.
--
-- What actually went wrong on 10-Aug was that ALL THREE of the day's
-- slots were spent on a single snapshot of the first ~21 minutes (itself
-- mostly a symptom of the Kite token being stale until then), not that
-- two entries arrived close together. order_manager.entry_reserved()
-- (replacing entry_paced(), removed) targets that directly: a cap on how
-- many of TODAY's entries may land before a cutoff clock time, with no
-- restriction at all on how close together they are otherwise. See that
-- function's own docstring and intraday/engine.py's two call sites for
-- the full reasoning.
--
-- swing_entry_min_gap_minutes / intraday_entry_min_gap_minutes are
-- REMOVED, not just deprecated -- confirmed by grep that entry_paced()
-- and both call sites reading them no longer exist, so leaving the rows
-- would repeat the exact "config key nobody reads" defect this session
-- fixed elsewhere (paper_starting_capital, migration 071) on the same
-- day it was found.
--
-- INTRADAY'S RESERVE DEFAULTS OFF (0), UNLIKE SWING'S (1). No verified
-- intraday incident of the same failure exists -- the direct check
-- (intraday_setups timing) is contaminated by pre-dedup duplicate rows,
-- and closed_positions has no entry-clock-time column, so this was left
-- unmeasured rather than guessed at symmetrically with swing. ORB-family
-- engines are also structurally supposed to cluster shortly after the
-- opening range closes; an early reserve would fight a genuine feature of
-- that engine without evidence it is needed. Raise
-- intraday_max_entries_before_time only with the same kind of evidence
-- swing_max_entries_before_time was built on.

DELETE FROM public.system_config
WHERE key IN ('swing_entry_min_gap_minutes', 'intraday_entry_min_gap_minutes');

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('swing_max_entries_before_time', '1',
   'How many of TODAY''s swing entries may land before '
   'swing_entries_time_cutoff. Verified against the 10-Aug-2026 incident: '
   'AUBANK and SCI were two of that day''s three best trades, approved '
   'together by the allocator in ONE scoring pass two seconds apart -- '
   'raising this to 2+ lets a genuine multi-name allocator decision '
   'through even before the cutoff; it only ever refuses a SEPARATE, '
   'early entry beyond the reserved count. 0 disables the reserve '
   '(count-only budget, the exact rollback lever). See '
   'execution/order_manager.py::entry_reserved.',
   'Master controls', 'intraday/engine.py', 'int', '1', 'MEDIUM'),
  ('swing_entries_time_cutoff', '10:30',
   'Clock time (HH:MM, IST) after which swing_max_entries_before_time '
   'stops applying entirely -- no restriction of any kind on entry timing '
   'past this point. An unparseable value disables the reserve (fails '
   'open, not closed).',
   'Master controls', 'intraday/engine.py', 'string', '10:30', 'LOW'),
  ('intraday_max_entries_before_time', '0',
   'Same mechanism as swing_max_entries_before_time, for the intraday '
   'book. Defaults OFF (0) -- no verified intraday incident of the same '
   'failure exists, and ORB-family engines are structurally supposed to '
   'cluster shortly after the opening range closes. Raise only with '
   'evidence, the same standard swing''s non-zero default was held to.',
   'Master controls', 'intraday/engine.py', 'int', '0', 'LOW'),
  ('intraday_entries_time_cutoff', '10:00',
   'Clock time (HH:MM, IST) cutoff for intraday_max_entries_before_time, '
   'inert while that key is 0.',
   'Master controls', 'intraday/engine.py', 'string', '10:00', 'LOW')
ON CONFLICT (key) DO NOTHING;
