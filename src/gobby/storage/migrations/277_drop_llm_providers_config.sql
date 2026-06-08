DELETE FROM config_store
 WHERE key = 'llm_providers'
    OR key LIKE 'llm_providers.%';
