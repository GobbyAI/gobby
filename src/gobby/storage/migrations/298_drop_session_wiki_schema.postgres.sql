-- Drop the legacy session-wiki schema. The session wiki page is now the
-- session summary (summary_markdown written to a flat file by the daemon), so
-- the second wiki narrative, its 11 sessions.wiki_* columns, and the
-- session_wiki_revisions table are removed. Idempotent and dependency-ordered:
-- FK -> indexes -> CHECK constraints -> columns -> table. Existing wiki data is
-- disposable (user-authorized destructive drop).

-- 1. Drop the sessions -> session_wiki_revisions foreign key first so the
--    wiki_revision_id column and the revisions table become droppable.
ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_wiki_revision_fk;

-- 2. Drop wiki indexes (table-scoped revision indexes plus the sessions
--    partial index on wiki_synthesis_consecutive_failures).
DROP INDEX IF EXISTS idx_session_wiki_revisions_session_created;
DROP INDEX IF EXISTS idx_session_wiki_revisions_previous;
DROP INDEX IF EXISTS idx_sessions_wiki_revision;
DROP INDEX IF EXISTS idx_sessions_wiki_synthesis_failures_source;

-- 3. Drop the wiki CHECK constraints on sessions.
ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_wiki_digest_turn_count_nonnegative;
ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_wiki_synthesis_consecutive_failures_nonnegative;

-- 4. Drop the 11 sessions.wiki_* columns.
ALTER TABLE sessions
    DROP COLUMN IF EXISTS wiki_path,
    DROP COLUMN IF EXISTS wiki_markdown,
    DROP COLUMN IF EXISTS wiki_revision_id,
    DROP COLUMN IF EXISTS wiki_source_context_hash,
    DROP COLUMN IF EXISTS wiki_digest_turn_count,
    DROP COLUMN IF EXISTS wiki_generation_mode,
    DROP COLUMN IF EXISTS wiki_generated_at,
    DROP COLUMN IF EXISTS wiki_synthesis_consecutive_failures,
    DROP COLUMN IF EXISTS wiki_synthesis_last_failure_reason,
    DROP COLUMN IF EXISTS wiki_synthesis_last_error,
    DROP COLUMN IF EXISTS wiki_synthesis_last_failed_at;

-- 5. Drop the revisions table (CASCADE clears any lingering dependents).
DROP TABLE IF EXISTS session_wiki_revisions CASCADE;
