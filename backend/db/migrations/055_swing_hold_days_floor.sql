-- allocation/swing_hold_days.py::expected_hold_days_by_family() excludes any
-- SWING engine family below this many resolved closes -- a mean needs far
-- less data to stabilise than the full R distribution Prior holds to a
-- 30-sample floor, so a smaller, separately-justified floor is deliberate
-- here, not a shortcut. Has a coded default (5) and works without this row;
-- inserted for the same reason every other threshold in this file gets a
-- row -- so it is discoverable and editable without reading the source.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('hold_days_min_sample', '5',
   'Minimum resolved closes before a SWING engine family gets its own '
   'per-family hold_days figure (allocation/swing_hold_days.py) instead of '
   'falling back to the book-pooled one. A mean is a simpler statistic than '
   'Prior''s full R distribution, so this floor is intentionally lower than '
   'the 30-sample floor used elsewhere -- not a shortcut, a separately '
   'justified number for a different kind of estimate. SWING only; '
   'intraday never reads this key.',
   'Master controls', 'allocation/swing_hold_days.py', 'int', '5', 'LOW')
ON CONFLICT (key) DO NOTHING;
