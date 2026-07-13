ALTER TABLE memories
DROP CONSTRAINT IF EXISTS memories_source_session_id_fkey;

ALTER TABLE memories
ADD CONSTRAINT memories_source_session_id_fkey
FOREIGN KEY (source_session_id)
REFERENCES sessions(id)
ON DELETE SET NULL
DEFERRABLE INITIALLY IMMEDIATE;
