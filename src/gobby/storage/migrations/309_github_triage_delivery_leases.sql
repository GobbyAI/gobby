-- Durable delivery leases and bounded retry scheduling for GitHub triage (#15974).
ALTER TABLE gh_triage_deliveries
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE gh_triage_deliveries
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_gh_triage_deliveries_retry
    ON gh_triage_deliveries(project_id, status, next_attempt_at, updated_at);
