-- gobby:non-transactional
-- Memory source_task index must not run inside a transaction.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_source_task ON memories USING btree (source_task_id);
