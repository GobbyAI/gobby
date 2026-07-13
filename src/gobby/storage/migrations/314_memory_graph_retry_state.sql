ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS graph_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS graph_status TEXT NOT NULL DEFAULT 'completed';

UPDATE memories
SET graph_status = CASE
    WHEN graph_status = 'failed' THEN 'failed'
    WHEN graph_processed IS FALSE THEN 'pending'
    ELSE 'completed'
END;

ALTER TABLE memories
    DROP CONSTRAINT IF EXISTS memories_graph_status_check;

ALTER TABLE memories
    ADD CONSTRAINT memories_graph_status_check
    CHECK (graph_status IN ('pending', 'completed', 'failed'));

CREATE INDEX IF NOT EXISTS idx_memories_graph_status_pending
    ON memories(created_at)
    WHERE graph_status = 'pending';
