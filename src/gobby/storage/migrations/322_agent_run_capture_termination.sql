ALTER TABLE agent_runs
    ADD COLUMN capture_id TEXT,
    ADD COLUMN capture_revision BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN pending_terminal_action TEXT,
    ADD COLUMN pending_terminal_reason TEXT,
    ADD COLUMN termination_requested_at TIMESTAMPTZ;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_pending_terminal_action_valid
    CHECK (
        pending_terminal_action IS NULL
        OR pending_terminal_action IN ('complete', 'fail', 'timeout', 'cancel')
    );

CREATE INDEX idx_agent_runs_pending_termination
ON agent_runs(termination_requested_at)
WHERE status IN ('pending', 'running')
  AND pending_terminal_action IS NOT NULL;
