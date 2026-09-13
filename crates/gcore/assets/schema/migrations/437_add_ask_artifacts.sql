CREATE TABLE ask_artifacts (
    execution_id uuid NOT NULL REFERENCES pipeline_executions(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind ~ '^[a-z0-9_-]+$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id, kind, sha256)
);

CREATE INDEX idx_ask_terminal_retention ON pipeline_executions (completed_at, id)
WHERE pipeline_name = 'native-ask' AND status IN ('completed', 'failed', 'cancelled');
