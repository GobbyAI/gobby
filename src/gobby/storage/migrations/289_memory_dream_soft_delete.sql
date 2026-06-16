-- Memory dream nightly self-healing GC: soft-delete + cooldown bookkeeping.
-- deleted_at NULL = visible; non-NULL = hidden (recoverable until purge).
-- dream_action records why dream hid the row ('review' | 'delete').
-- last_dreamed_at is the cooldown cursor that makes the active sweep idempotent.
-- Mirrors postgres_baseline_schema.sql (CREATE TABLE memories); names match in
-- both. Idempotent because the baseline snapshot already carries these objects
-- on fresh installs, where post-baseline migrations re-run on top.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS dream_action TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_dreamed_at TIMESTAMPTZ;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'memories_dream_action_check'
    ) THEN
        ALTER TABLE memories
            ADD CONSTRAINT memories_dream_action_check
            CHECK (dream_action IS NULL OR dream_action IN ('review', 'delete'));
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'memories_dream_action_requires_deleted'
    ) THEN
        ALTER TABLE memories
            ADD CONSTRAINT memories_dream_action_requires_deleted
            CHECK (dream_action IS NULL OR deleted_at IS NOT NULL);
    END IF;
END $$;

-- Candidate sweep: scan visible rows ordered by cooldown cursor then recency.
CREATE INDEX IF NOT EXISTS idx_memories_dream_candidates
    ON memories (last_dreamed_at, updated_at)
    WHERE deleted_at IS NULL;

-- Purge sweep: find hidden rows by action + age.
CREATE INDEX IF NOT EXISTS idx_memories_dream_purge
    ON memories (dream_action, deleted_at)
    WHERE deleted_at IS NOT NULL;
