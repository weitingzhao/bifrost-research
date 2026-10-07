-- TD-160 before/after. Read-only. Run once before the DROP and once after.
-- Before: four rows, and each old index's indkey/indpred equals its replacement.
-- After: the two event_radar_* secondary indexes are gone; the two
-- event_signal_radar_daily_* indexes and event_radar_pkey remain.

SELECT c.relname AS index_name,
       pg_get_indexdef(i.indexrelid) AS definition,
       i.indkey::text AS indkey,
       coalesce(pg_get_expr(i.indpred, i.indrelid), '') AS predicate,
       pg_size_pretty(pg_relation_size(i.indexrelid)) AS size
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'features'
  AND t.relname = 'event_signal_radar_daily'
ORDER BY 1;
