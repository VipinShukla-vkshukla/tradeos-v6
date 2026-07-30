-- 028_symbol_product_key.sql
--
-- Let one symbol be held by BOTH books at once, keyed on (symbol, product).
--
-- WHY
-- ---
-- open_positions was unique on symbol alone, so a name held by swing could not
-- also be traded intraday: the second entry upserted over the first — entry
-- price, stop, target, framework and the R baseline — and the 15:15 square-off
-- then sold a multi-week thesis because the row said INTRADAY. Four entry paths
-- guarded against that by refusing the second entry, which prevented corruption
-- and cost the opportunity.
--
-- A professional desk holds a swing core and trades around it. The case where
-- both frameworks independently like a name is the case with the MOST
-- conviction, and refusing it left money on the table exactly there.
--
-- WHY (symbol, product) AND NOT A TRADE-ID ATTRIBUTION LAYER
-- ----------------------------------------------------------
-- The hard part looked like reconciliation: broker shows 5 CIPLA, book says
-- swing 2 plus intraday 3, broker drops to 3 — whose shares went? That appears
-- to need fill-level attribution.
--
-- It does not, because Kite already separates them. positions() returns
-- CIPLA-CNC and CIPLA-MIS as distinct rows with distinct quantities. Swing is
-- CNC by construction (a multi-week position must never be MIS, which the
-- broker liquidates at 15:20 the day it opens) and intraday becomes MIS, so the
-- two tranches are distinct AT THE SOURCE rather than in an attribution layer
-- this project would then have to keep correct forever.
--
-- ACCEPTED COST
-- -------------
-- MIS has no GTT — Kite GTTs are CNC/NRML only, so an intraday tranche loses its
-- broker-side resting stop. That is a swap rather than a loss: MIS is
-- force-squared by the broker around 15:20, which provides what the GTT
-- provided, protection that survives the daemon dying. Intraday is flat by
-- 15:15 under its own policy in any case. Swing keeps CNC and keeps its GTTs;
-- nothing about the swing book changes.

-- ── 1. product must be present and known before it can be part of a key ────
UPDATE open_positions
   SET product = 'CNC'
 WHERE product IS NULL OR btrim(product) = '';

ALTER TABLE open_positions
    ALTER COLUMN product SET DEFAULT 'CNC';

-- ── 2. Replace the symbol-only uniqueness ─────────────────────────────────
-- Written as a scan rather than a named DROP because the constraint has been
-- created under different names across environments, and a migration that fails
-- on a name mismatch is one that gets edited by hand and then diverges.
DO $$
DECLARE r RECORD;
BEGIN
    -- Both PRIMARY KEY and UNIQUE, because symbol turned out to be the primary
    -- key here, not merely a unique constraint. A PK on symbol alone is exactly
    -- the thing that makes two tranches impossible, so it has to be replaced
    -- rather than worked around.
    FOR r IN
        SELECT c.conname, c.contype
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'open_positions'
           AND c.contype IN ('p', 'u')
           -- attname is `name`, not `text`. Comparing name[] to a text[]
           -- literal fails with 42883 operator does not exist.
           AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
                  FROM unnest(c.conkey) k
                  JOIN pg_attribute a
                    ON a.attrelid = c.conrelid AND a.attnum = k)
               = ARRAY['symbol']::text[]
    LOOP
        EXECUTE format('ALTER TABLE open_positions DROP CONSTRAINT %I', r.conname);
        RAISE NOTICE 'dropped % constraint % on symbol alone',
            CASE r.contype WHEN 'p' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END, r.conname;
    END LOOP;

    -- Standalone unique indexes only. An index that BACKS a constraint cannot
    -- be dropped directly — Postgres refuses with 2BP01 and tells you to drop
    -- the constraint, which the loop above already did. Filtering on
    -- conindid IS NULL leaves only indexes created independently.
    FOR r IN
        SELECT i.relname AS iname
          FROM pg_index x
          JOIN pg_class i ON i.oid = x.indexrelid
          JOIN pg_class t ON t.oid = x.indrelid
         WHERE t.relname = 'open_positions'
           AND x.indisunique
           AND NOT x.indisprimary
           AND x.indnatts = 1
           AND NOT EXISTS (SELECT 1 FROM pg_constraint c
                            WHERE c.conindid = x.indexrelid)
           AND (SELECT a.attname::text FROM pg_attribute a
                 WHERE a.attrelid = x.indrelid AND a.attnum = x.indkey[0]) = 'symbol'
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %I', r.iname);
        RAISE NOTICE 'dropped standalone unique index %', r.iname;
    END LOOP;
END $$;

-- ON CONFLICT needs a real unique constraint over exactly these columns.
-- Not a partial index: PostgREST upserts cannot target one.
-- product cannot be NULL once it is part of the key.
ALTER TABLE open_positions
    ALTER COLUMN product SET NOT NULL;

ALTER TABLE open_positions
    ADD CONSTRAINT open_positions_symbol_product_key UNIQUE (symbol, product);

-- Restore a primary key if dropping the symbol-only one left the table without
-- one. A table with no PK still works, but replication and several tools assume
-- one exists, and the composite is the honest identity now.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c
                     JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = 'open_positions' AND c.contype = 'p') THEN
        ALTER TABLE open_positions
            ADD CONSTRAINT open_positions_pkey PRIMARY KEY (symbol, product);
        RAISE NOTICE 'primary key restored on (symbol, product)';
    END IF;
END $$;

COMMENT ON COLUMN open_positions.product IS
    'CNC or MIS. Half the uniqueness key — one symbol may be held once per '
    'product, which is what lets a swing CNC core coexist with an intraday MIS '
    'tranche. Swing is always CNC; intraday follows intraday_product.';

-- ── 3. Make the two books actually distinguishable ────────────────────────
-- Pointless without this: two CNC tranches on one symbol still collide.
UPDATE system_config
   SET value = 'MIS', updated_at = NOW()
 WHERE key = 'intraday_product';
