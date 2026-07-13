ALTER TABLE memories
ADD COLUMN IF NOT EXISTS vector_needs_reindex BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_memories_vector_needs_reindex
    ON memories(id)
    WHERE vector_needs_reindex IS TRUE;
