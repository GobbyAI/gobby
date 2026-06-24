-- Migration 297: track consecutive wiki synthesis failures on sessions.
--
-- The wiki artifact is generated from LLM output. Repeated failures should be
-- durable and visible in health/status surfaces without persisting invalid
-- markdown. Columns live on sessions because the degradation is per source
-- session and is reset by successful wiki persistence.

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS wiki_synthesis_consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_synthesis_last_failure_reason TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_synthesis_last_error TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_synthesis_last_failed_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'sessions_wiki_synthesis_consecutive_failures_nonnegative'
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_wiki_synthesis_consecutive_failures_nonnegative
            CHECK (wiki_synthesis_consecutive_failures >= 0);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_sessions_wiki_synthesis_failures_source
    ON sessions(source)
    WHERE wiki_synthesis_consecutive_failures > 0;
