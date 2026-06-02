WITH source_secret AS (
    SELECT name, encrypted_value, category, created_at
    FROM secrets
    WHERE name IN ('embeddings_api_key', 'api_key', 'openai_api_key')
    ORDER BY CASE name
        WHEN 'embeddings_api_key' THEN 0
        WHEN 'api_key' THEN 1
        WHEN 'openai_api_key' THEN 2
        ELSE 3
    END
    LIMIT 1
)
INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
SELECT
    'secret-' || md5(source_secret.name || ':embeddings_api_key'),
    'embeddings_api_key',
    source_secret.encrypted_value,
    source_secret.category,
    'Config secret for ai.embeddings.api_key',
    source_secret.created_at,
    NOW()
FROM source_secret
ON CONFLICT (name) DO NOTHING;

INSERT INTO config_store (key, value, source, is_secret, updated_at)
SELECT
    CASE key
        WHEN 'embeddings.api_base' THEN 'ai.embeddings.api_base'
        WHEN 'embeddings.model' THEN 'ai.embeddings.model'
        WHEN 'embeddings.dim' THEN 'ai.embeddings.dim'
        WHEN 'embeddings.query_prefix' THEN 'ai.embeddings.query_prefix'
        WHEN 'embeddings.provider' THEN 'ai.embeddings.provider'
    END,
    value,
    source,
    is_secret,
    NOW()
FROM config_store
WHERE key IN (
    'embeddings.api_base',
    'embeddings.model',
    'embeddings.dim',
    'embeddings.query_prefix',
    'embeddings.provider'
)
ON CONFLICT (key) DO NOTHING;

WITH api_key_source AS (
    SELECT
        '"$secret:embeddings_api_key"' AS value,
        source,
        TRUE AS is_secret,
        0 AS priority
    FROM config_store
    WHERE key = 'embeddings.api_key'
      AND EXISTS (
          SELECT 1
          FROM secrets
          WHERE name = 'embeddings_api_key'
      )
    UNION ALL
    SELECT
        '"$secret:embeddings_api_key"',
        'user',
        TRUE,
        1
    FROM secrets
    WHERE name = 'embeddings_api_key'
),
selected_api_key_source AS (
    SELECT value, source, is_secret
    FROM api_key_source
    ORDER BY priority
    LIMIT 1
)
INSERT INTO config_store (key, value, source, is_secret, updated_at)
SELECT
    'ai.embeddings.api_key',
    value,
    source,
    is_secret,
    NOW()
FROM selected_api_key_source
ON CONFLICT (key) DO NOTHING;

DELETE FROM config_store
WHERE key IN (
    'embeddings.api_base',
    'embeddings.model',
    'embeddings.api_key',
    'embeddings.dim',
    'embeddings.query_prefix',
    'embeddings.provider'
);
