-- Prevent self-parent session lineage cycles.

UPDATE sessions
   SET parent_session_id = NULL,
       updated_at = NOW()
 WHERE parent_session_id = id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'sessions_parent_session_not_self'
           AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_parent_session_not_self
            CHECK (parent_session_id IS NULL OR parent_session_id <> id);
    END IF;
END
$$;
