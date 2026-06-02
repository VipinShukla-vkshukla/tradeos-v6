---3. JSON high-level summary
--The compute_meta JSON already contains a summary per symbol, but there's no cross-symbol aggregate stored anywhere. 
--The closest you can get right now without changing the script is this query in Supabase — it unpacks the JSON and gives you a field-level match rate across all 500 symbols:

SELECT
    field,
    COUNT(*) AS symbols,
    ROUND(COUNT(*) * 100.0 / 500, 1) AS match_pct
FROM stock_data_daily,
     jsonb_array_elements_text(
         (compute_meta::jsonb -> 'computed_match')
     ) AS field
WHERE date = '2026-03-27'
GROUP BY field
ORDER BY symbols DESC;

--And for diverged fields:
SELECT
    field,
    COUNT(*) AS symbols,
    ROUND(AVG((compute_meta::jsonb -> 'diverged' -> field ->> 'delta_pct')::numeric), 1) AS avg_delta_pct
FROM stock_data_daily,
     jsonb_object_keys(compute_meta::jsonb -> 'diverged') AS field
WHERE date = '2026-03-27'
GROUP BY field
ORDER BY avg_delta_pct DESC;

--Export TABLE CREATION (DDL-like output)
SELECT 
    table_name,
    'CREATE TABLE ' || table_name || ' (' ||
    string_agg(
        column_name || ' ' || data_type ||
        CASE 
            WHEN is_nullable = 'NO' THEN ' NOT NULL' 
            ELSE '' 
        END,
        ', '
    ) || ');' AS create_statement
FROM information_schema.columns
WHERE table_schema = 'public'
GROUP BY table_name;

--👉 This approximates:
--CREATE TABLE statements
--⚠️ Note:
--Doesn’t include indexes, constraints, PK/FK

--3. Export PRIMARY KEYS
SELECT
    tc.table_name,
    kc.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kc
    ON tc.constraint_name = kc.constraint_name
WHERE tc.constraint_type = 'PRIMARY KEY'
AND tc.table_schema = 'public';

--Export FOREIGN KEYS
SELECT
    tc.table_name,
    kcu.column_name,
    ccu.table_name AS foreign_table,
    ccu.column_name AS foreign_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
AND tc.table_schema = 'public';

--Export INDEXES
SELECT
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE schemaname = 'public';

---🚀 BEST PRACTICAL METHOD (Recommended)
supabase db dump --file full_backup.sql

--Option B: pg_dump (Production-grade)
pg_dump "postgresql://USER:PASSWORD@HOST:PORT/postgres" > backup.sql

--If your goal is “get everything in one result”, you can combine metadata into ONE JSON output:
WITH tables AS (
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public'
),
columns AS (
    SELECT table_name, json_agg(
        json_build_object(
            'column', column_name,
            'type', data_type,
            'nullable', is_nullable,
            'default', column_default
        )
    ) AS cols
    FROM information_schema.columns
    WHERE table_schema = 'public'
    GROUP BY table_name
),
indexes AS (
    SELECT tablename AS table_name,
           json_agg(indexdef) AS idx
    FROM pg_indexes
    WHERE schemaname = 'public'
    GROUP BY tablename
)
SELECT json_build_object(
    'tables', json_agg(
        json_build_object(
            'table', t.table_name,
            'columns', c.cols,
            'indexes', i.idx
        )
    )
)
FROM tables t
LEFT JOIN columns c ON t.table_name = c.table_name
LEFT JOIN indexes i ON t.table_name = i.table_name;