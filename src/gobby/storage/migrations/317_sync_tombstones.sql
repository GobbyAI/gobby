CREATE TABLE IF NOT EXISTS sync_tombstones (
    entity_type TEXT NOT NULL CHECK (entity_type IN ('task', 'memory')),
    entity_id UUID NOT NULL,
    project_id UUID,
    deleted_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_tombstones_project
    ON sync_tombstones(entity_type, project_id);

CREATE OR REPLACE FUNCTION capture_sync_tombstone()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO sync_tombstones (entity_type, entity_id, project_id, deleted_at)
    VALUES (TG_ARGV[0], OLD.id, OLD.project_id, CURRENT_TIMESTAMP)
    ON CONFLICT (entity_type, entity_id) DO UPDATE
        SET project_id = EXCLUDED.project_id,
            deleted_at = GREATEST(sync_tombstones.deleted_at, EXCLUDED.deleted_at);
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS tasks_capture_sync_tombstone ON tasks;
CREATE TRIGGER tasks_capture_sync_tombstone
BEFORE DELETE ON tasks
FOR EACH ROW EXECUTE FUNCTION capture_sync_tombstone('task');

DROP TRIGGER IF EXISTS memories_capture_sync_tombstone ON memories;
CREATE TRIGGER memories_capture_sync_tombstone
BEFORE DELETE ON memories
FOR EACH ROW EXECUTE FUNCTION capture_sync_tombstone('memory');
