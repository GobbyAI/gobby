ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS resume_metadata_json JSONB;
