ALTER TABLE code_indexed_project_states
ADD COLUMN IF NOT EXISTS indexer_version text;
