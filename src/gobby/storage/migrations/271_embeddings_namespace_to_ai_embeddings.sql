WITH source_secret AS (
    SELECT id, encrypted_value, category, created_at
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
    source_secret.id || ':embeddings_api_key',
    'embeddings_api_key',
    source_secret.encrypted_value,
    source_secret.category,
    'Config secret for ai.embeddings.api_key',
    source_secret.created_at,
    NOW()
FROM source_secret
WHERE NOT EXISTS (SELECT 1 FROM secrets WHERE name = 'embeddings_api_key')
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
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    source = EXCLUDED.source,
    is_secret = EXCLUDED.is_secret,
    updated_at = EXCLUDED.updated_at;

INSERT INTO config_store (key, value, source, is_secret, updated_at)
SELECT
    'ai.embeddings.api_key',
    to_json('$secret:embeddings_api_key'::text)::text,
    source,
    TRUE,
    NOW()
FROM config_store
WHERE key = 'embeddings.api_key'
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    source = EXCLUDED.source,
    is_secret = TRUE,
    updated_at = EXCLUDED.updated_at;

DELETE FROM config_store
WHERE key IN (
    'embeddings.api_base',
    'embeddings.model',
    'embeddings.api_key',
    'embeddings.dim',
    'embeddings.query_prefix',
    'embeddings.provider'
);
