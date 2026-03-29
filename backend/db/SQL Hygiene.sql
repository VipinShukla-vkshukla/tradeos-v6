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