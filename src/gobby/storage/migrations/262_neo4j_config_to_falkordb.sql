INSERT INTO config_store (key, value, source, is_secret, updated_at)
SELECT REPLACE(key, 'databases.neo4j.', 'databases.falkordb.'),
       value,
       source,
       is_secret,
       updated_at
  FROM config_store
 WHERE key IN (
       'databases.neo4j.graph_search',
       'databases.neo4j.graph_min_score',
       'databases.neo4j.rrf_k',
       'databases.neo4j.graph_name'
 )
ON CONFLICT (key) DO NOTHING;

DELETE FROM config_store
 WHERE key LIKE 'databases.neo4j.%';

DELETE FROM secrets
 WHERE name = 'auth'
   AND NOT EXISTS (
       SELECT 1
         FROM config_store
        WHERE value = to_json('$secret:auth'::text)::text
   );
