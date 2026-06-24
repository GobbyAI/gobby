-- Migration 296: session knowledge-synthesis (wiki) artifact.
--
-- Adds a durable, cross-linked per-session wiki page synthesized from the
-- per-turn digest. Mirrors the summary-revision wiring already in the baseline
-- (sessions.summary_* columns + session_summary_revisions): wiki_* columns on
-- sessions, a dedicated session_wiki_revisions table, a composite FK
-- (wiki_revision_id, id) -> session_wiki_revisions(id, session_id) so a session
-- can only point at one of its OWN revisions, and supporting indexes.
--
-- Idempotent on purpose: postgres_baseline_schema.sql is a CURRENT snapshot
-- applied first on a fresh DB, then this migration runs on top. Every statement
-- guards against pre-existing objects (ADD COLUMN IF NOT EXISTS /
-- CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / DO-block guards for
-- named constraints), so it is a no-op on a freshly baselined DB and applies the
-- delta on an existing one. The runner splits on dollar-quote-aware boundaries,
-- so DO $$ ... $$ blocks are safe.

-- 1. Wiki columns on sessions (traceability parity with summary_*).
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_path TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_markdown TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_revision_id TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_source_context_hash TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_digest_turn_count INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_generation_mode TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wiki_generated_at TIMESTAMPTZ;

-- 2. Nonnegative guard on sessions.wiki_digest_turn_count (mirrors summary).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'sessions_wiki_digest_turn_count_nonnegative'
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_wiki_digest_turn_count_nonnegative
            CHECK (wiki_digest_turn_count IS NULL OR wiki_digest_turn_count >= 0);
    END IF;
END
$$;

-- 3. Dedicated wiki-revision table (do NOT overload session_summary_revisions:
--    different artifact, different prompt, different owner field).
CREATE TABLE IF NOT EXISTS session_wiki_revisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    wiki_markdown TEXT NOT NULL,
    generation_mode TEXT NOT NULL,
    source_context_hash TEXT,
    digest_turn_count INTEGER,
    previous_revision_id TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT session_wiki_revisions_digest_turn_count_nonnegative
    CHECK (digest_turn_count IS NULL OR digest_turn_count >= 0),
    CONSTRAINT session_wiki_revisions_generation_mode_valid
    CHECK (
    generation_mode IN ('agent_authored', 'full', 'delta', 'digest_fallback', 'noop')
    ),
    CONSTRAINT session_wiki_revisions_id_session_id_unique
    UNIQUE (id, session_id),
    CONSTRAINT session_wiki_revisions_previous_same_session_fk
    FOREIGN KEY (previous_revision_id, session_id)
    REFERENCES session_wiki_revisions(id, session_id)
    ON DELETE SET NULL (previous_revision_id)
    DEFERRABLE INITIALLY IMMEDIATE
);

-- 4. Composite FK: a session's wiki revision must belong to that session.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'sessions_wiki_revision_fk'
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_wiki_revision_fk
            FOREIGN KEY (wiki_revision_id, id)
            REFERENCES session_wiki_revisions(id, session_id)
            ON DELETE SET NULL (wiki_revision_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END
$$;

-- 5. Indexes.
CREATE INDEX IF NOT EXISTS idx_session_wiki_revisions_session_created
    ON session_wiki_revisions(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_wiki_revisions_previous
    ON session_wiki_revisions(previous_revision_id);
CREATE INDEX IF NOT EXISTS idx_sessions_wiki_revision ON sessions(wiki_revision_id);
