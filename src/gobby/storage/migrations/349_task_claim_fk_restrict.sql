ALTER TABLE tasks
DROP CONSTRAINT IF EXISTS tasks_claimed_by_session_id_fkey;

ALTER TABLE tasks
ADD CONSTRAINT tasks_claimed_by_session_id_fkey
FOREIGN KEY (claimed_by_session_id)
REFERENCES sessions(id)
ON DELETE RESTRICT
DEFERRABLE INITIALLY IMMEDIATE;
