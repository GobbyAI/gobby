-- Expired event details can be discarded only after their usage is preserved.
CREATE INDEX IF NOT EXISTS idx_token_events_event_at ON token_events (event_at, id);

CREATE TABLE token_event_retention_totals (
    session_id uuid PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_creation_tokens bigint NOT NULL DEFAULT 0 CHECK (cache_creation_tokens >= 0),
    cache_read_tokens bigint NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE token_event_retention_totals
    TO gobby_daemon_runtime;
