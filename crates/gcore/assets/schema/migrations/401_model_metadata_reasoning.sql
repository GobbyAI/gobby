ALTER TABLE model_metadata
    ADD COLUMN IF NOT EXISTS reasoning_present boolean,
    ADD COLUMN IF NOT EXISTS reasoning_supported_efforts jsonb,
    ADD COLUMN IF NOT EXISTS reasoning_default_effort text,
    ADD COLUMN IF NOT EXISTS reasoning_default_enabled boolean,
    ADD COLUMN IF NOT EXISTS reasoning_mandatory boolean;
