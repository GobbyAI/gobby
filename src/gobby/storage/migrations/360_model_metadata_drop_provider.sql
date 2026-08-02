-- gobby:destructive

-- model_metadata loses its provider column: model metadata is
-- provider-independent, so the (provider, model) PK misattributed shared
-- models and dropped unknown vendors. model becomes the PRIMARY KEY.
-- Existing rows are cache — deleted here and repopulated from the OpenRouter
-- registry at daemon startup/refresh.
DELETE FROM model_metadata;

ALTER TABLE model_metadata DROP CONSTRAINT IF EXISTS model_metadata_pkey;
ALTER TABLE model_metadata DROP COLUMN IF EXISTS provider;
ALTER TABLE model_metadata ADD PRIMARY KEY (model);
