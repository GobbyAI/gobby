-- Separate memory ownership from cross-project visibility.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE memories
SET project_id = '00000000-0000-0000-0000-000000060887'::uuid,
    is_global = TRUE
WHERE project_id IS NULL;

-- Every pre-migration secondary point/node lacks the explicit visibility bit.
UPDATE memories
SET vector_needs_reindex = TRUE,
    graph_processed = FALSE,
    graph_status = 'pending',
    graph_attempts = 0;

ALTER TABLE memories
    ALTER COLUMN project_id SET NOT NULL;

ALTER TABLE memories
    DROP CONSTRAINT IF EXISTS memories_project_id_fkey;

ALTER TABLE memories
    ADD CONSTRAINT memories_project_id_fkey
    FOREIGN KEY (project_id)
    REFERENCES projects(id)
    ON DELETE RESTRICT
    DEFERRABLE INITIALLY IMMEDIATE;

DROP INDEX IF EXISTS idx_memories_project;
DROP INDEX IF EXISTS idx_memories_project_live;
DROP INDEX IF EXISTS idx_memories_global_live;

CREATE INDEX idx_memories_project_live
    ON memories(project_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_memories_global_live
    ON memories(updated_at DESC)
    WHERE is_global IS TRUE AND deleted_at IS NULL;
