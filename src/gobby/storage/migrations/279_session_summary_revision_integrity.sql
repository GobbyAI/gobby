-- Enforce same-session integrity for summary revision pointers.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'sessions_summary_digest_turn_count_nonnegative'
           AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_summary_digest_turn_count_nonnegative
            CHECK (summary_digest_turn_count IS NULL OR summary_digest_turn_count >= 0);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'session_summary_revisions_id_session_id_unique'
           AND conrelid = 'session_summary_revisions'::regclass
    ) THEN
        ALTER TABLE session_summary_revisions
            ADD CONSTRAINT session_summary_revisions_id_session_id_unique
            UNIQUE (id, session_id);
    END IF;
END $$;

UPDATE session_summary_revisions child
   SET previous_revision_id = NULL
 WHERE previous_revision_id IS NOT NULL
   AND NOT EXISTS (
       SELECT 1
         FROM session_summary_revisions parent
        WHERE parent.id = child.previous_revision_id
          AND parent.session_id = child.session_id
   );

ALTER TABLE session_summary_revisions
    DROP CONSTRAINT IF EXISTS session_summary_revisions_previous_revision_id_fkey;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'session_summary_revisions_previous_same_session_fk'
           AND conrelid = 'session_summary_revisions'::regclass
    ) THEN
        ALTER TABLE session_summary_revisions
            ADD CONSTRAINT session_summary_revisions_previous_same_session_fk
            FOREIGN KEY (previous_revision_id, session_id)
            REFERENCES session_summary_revisions(id, session_id)
            ON DELETE SET NULL (previous_revision_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

UPDATE sessions s
   SET summary_revision_id = NULL
 WHERE summary_revision_id IS NOT NULL
   AND NOT EXISTS (
       SELECT 1
         FROM session_summary_revisions r
        WHERE r.id = s.summary_revision_id
          AND r.session_id = s.id
   );

ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_summary_revision_fk;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'sessions_summary_revision_fk'
           AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_summary_revision_fk
            FOREIGN KEY (summary_revision_id, id)
            REFERENCES session_summary_revisions(id, session_id)
            ON DELETE SET NULL (summary_revision_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;
