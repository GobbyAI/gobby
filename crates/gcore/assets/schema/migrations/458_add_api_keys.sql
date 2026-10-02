-- User-issued, machine-bound API keys. Only the SHA-256 of a key is stored.
CREATE TABLE api_keys (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id uuid NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    key_hash text NOT NULL UNIQUE,
    key_hint text NOT NULL,
    label text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX idx_api_keys_machine ON api_keys (machine_id);
ALTER TABLE machines ADD COLUMN last_heartbeat_at timestamptz;
ALTER TABLE machines ADD COLUMN node_version text;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE api_keys TO gobby_daemon_runtime;
