DO $$
BEGIN
    IF to_regclass('model_metadata') IS NULL AND to_regclass('model_costs') IS NOT NULL THEN
        ALTER TABLE model_costs RENAME TO model_metadata;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = to_regclass('model_metadata')
          AND conname = 'model_costs_pkey'
    ) THEN
        ALTER TABLE model_metadata
        RENAME CONSTRAINT model_costs_pkey TO model_metadata_pkey;
    END IF;
END
$$;
