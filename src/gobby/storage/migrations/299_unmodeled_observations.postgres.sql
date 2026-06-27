CREATE TABLE IF NOT EXISTS unmodeled_observation_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    server_name TEXT NOT NULL DEFAULT '',
    tool_type TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    source_line INTEGER,
    sample_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    sample_hash TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        session_id,
        source,
        kind,
        name,
        server_name,
        tool_type,
        source_ref,
        sample_hash
    )
);

CREATE TABLE IF NOT EXISTS unmodeled_observations (
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    server_name TEXT NOT NULL DEFAULT '',
    tool_type TEXT NOT NULL DEFAULT '',
    count BIGINT NOT NULL DEFAULT 0 CHECK (count >= 0),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    example_session_id TEXT NOT NULL,
    sample_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    sample_hash TEXT NOT NULL,
    PRIMARY KEY (source, kind, name, server_name, tool_type)
);

CREATE INDEX IF NOT EXISTS idx_unmodeled_observation_events_last_seen
    ON unmodeled_observation_events(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_unmodeled_observations_worklist
    ON unmodeled_observations(count DESC, last_seen_at DESC);
