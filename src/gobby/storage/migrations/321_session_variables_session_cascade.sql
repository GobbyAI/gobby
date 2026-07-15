DELETE FROM session_variables AS variables
WHERE NOT EXISTS (
    SELECT 1
    FROM sessions
    WHERE sessions.id = variables.session_id
);

ALTER TABLE session_variables
DROP CONSTRAINT IF EXISTS session_variables_session_id_fkey;

ALTER TABLE session_variables
ADD CONSTRAINT session_variables_session_id_fkey
FOREIGN KEY (session_id)
REFERENCES sessions(id)
ON DELETE CASCADE
DEFERRABLE INITIALLY IMMEDIATE;
