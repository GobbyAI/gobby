-- Context usage snapshot tracking
-- Adds normalized fields for current context pressure and provider-specific token tracking

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_used_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_usage_ratio NUMERIC(5, 4);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_usage_source TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_usage_confidence TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_usage_updated_at TIMESTAMPTZ;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_prompt_input_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_prompt_uncached_input_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_prompt_cache_read_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_prompt_cache_creation_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_completion_output_tokens INTEGER;

CREATE INDEX IF NOT EXISTS idx_sessions_context_usage_ratio ON sessions(context_usage_ratio DESC)
    WHERE context_usage_ratio IS NOT NULL;
