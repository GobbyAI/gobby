-- Store provider detection manifests with explicit bundled/user ownership.
CREATE TABLE IF NOT EXISTS detection_manifests (
    provider_id TEXT PRIMARY KEY CHECK (provider_id ~ '^[a-z][a-z0-9_-]*$'),
    version TEXT NOT NULL CHECK (version ~ '^[0-9]+(\.[0-9]+)*$'),
    engine INTEGER NOT NULL CHECK (engine > 0),
    content TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('bundled', 'user')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
