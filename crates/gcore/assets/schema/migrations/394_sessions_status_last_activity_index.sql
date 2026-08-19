-- gobby:non-transactional
-- Sessions status/last_activity index must not run inside a transaction.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sessions_status_last_activity ON sessions (status, last_activity);
