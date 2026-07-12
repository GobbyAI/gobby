DO $$
DECLARE
    missing_provider_count BIGINT;
BEGIN
    SELECT COUNT(*)
      INTO missing_provider_count
      FROM model_costs
     WHERE provider IS NULL OR BTRIM(provider) = '';

    IF missing_provider_count > 0 THEN
        RAISE EXCEPTION
            'model_costs provider-key preflight failed: % row(s) have no provider',
            missing_provider_count;
    END IF;
END $$;

ALTER TABLE model_costs
    DROP CONSTRAINT IF EXISTS model_costs_pkey;

ALTER TABLE model_costs
    ALTER COLUMN model SET NOT NULL,
    ALTER COLUMN provider SET NOT NULL;

ALTER TABLE model_costs
    ADD CONSTRAINT model_costs_pkey PRIMARY KEY (provider, model);
