-- Track source-aware session handoff summary revisions.

CREATE TABLE IF NOT EXISTS session_summary_revisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    summary_markdown TEXT NOT NULL,
    generation_mode TEXT NOT NULL,
    source_context_hash TEXT,
    source_digest_turn_count INTEGER,
    previous_revision_id TEXT REFERENCES session_summary_revisions(id) ON DELETE SET NULL DEFERRABLE INITIALLY IMMEDIATE,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT session_summary_revisions_digest_turn_count_nonnegative
        CHECK (source_digest_turn_count IS NULL OR source_digest_turn_count >= 0)
);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_revision_id TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_source_context_hash TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_digest_turn_count INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_generation_mode TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_generated_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'sessions_summary_revision_fk'
           AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_summary_revision_fk
            FOREIGN KEY (summary_revision_id)
            REFERENCES session_summary_revisions(id)
            ON DELETE SET NULL
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_session_summary_revisions_session_created
    ON session_summary_revisions(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_summary_revisions_previous
    ON session_summary_revisions(previous_revision_id);

CREATE INDEX IF NOT EXISTS idx_sessions_summary_revision
    ON sessions(summary_revision_id);
