ALTER TABLE tasks
    ADD COLUMN delegated_to_session_id uuid REFERENCES sessions(id) ON DELETE SET NULL,
    ADD COLUMN delegated_by_session_id uuid REFERENCES sessions(id) ON DELETE SET NULL,
    ADD COLUMN delegation_reason text,
    ADD COLUMN delegated_at timestamp with time zone;
