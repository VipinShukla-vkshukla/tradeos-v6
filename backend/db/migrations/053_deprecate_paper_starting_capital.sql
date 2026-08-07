-- paper_starting_capital was introduced 31-Jul-2026 in execution/paper_broker.py
-- ::capacity(), before config.capital_for()'s book-capital-sleeve mechanism
-- existed at all (built 05/06-Aug-2026) -- and was never migrated onto it once
-- it did. Confirmed live 07-Aug-2026: paper_starting_capital sat at Rs 20,000
-- against an intraday_capital of Rs 1,00,000 (the dashboard's own "Capital"
-- field for intraday), silently capping paper deployment at a fifth of what
-- the operator could see was configured, with nothing connecting the two
-- numbers anywhere in the code or the UI.
--
-- capacity() now reads capital_for("INTRADAY") directly (same commit as this
-- migration). This key is no longer read anywhere. Not deleted -- a row that
-- silently disappears is exactly the "did my change even apply" question this
-- project's own operator panel re-reads after every write to avoid -- only
-- marked, so a future session finds an explanation instead of a mystery key.

UPDATE public.system_config
SET description = 'DEPRECATED 07-Aug-2026, no longer read by any code path. '
                   'execution/paper_broker.py::capacity() used this as an '
                   'independent paper-capital ceiling, separate from '
                   'intraday_capital (config.capital_for) -- the two could '
                   'silently drift apart, and did (this sat at 20000 against '
                   'an intraday_capital of 100000). capacity() now reads '
                   'capital_for("INTRADAY") directly, the same number the '
                   'live sizing formula uses, so there is nothing left for '
                   'this key to independently control. Safe to ignore; kept '
                   'rather than deleted so its history is not silently lost.'
WHERE key = 'paper_starting_capital';
