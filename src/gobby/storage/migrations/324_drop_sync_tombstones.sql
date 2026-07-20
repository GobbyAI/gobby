DROP TRIGGER IF EXISTS tasks_capture_sync_tombstone ON tasks;
DROP TRIGGER IF EXISTS memories_capture_sync_tombstone ON memories;
DROP FUNCTION IF EXISTS capture_sync_tombstone();
DROP TABLE IF EXISTS sync_tombstones;
