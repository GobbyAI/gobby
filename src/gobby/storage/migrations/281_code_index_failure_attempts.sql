-- Track failed code-index queue attempts so permanent failures do not pin batch heads.

ALTER TABLE code_indexed_files
    ADD COLUMN IF NOT EXISTS vector_sync_attempted_at TIMESTAMPTZ;

ALTER TABLE code_symbols
    ADD COLUMN IF NOT EXISTS summary_attempted_at TIMESTAMPTZ;
