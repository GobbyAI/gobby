-- Per-project external issue sync configuration and durable reconciliation health.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS linear_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE projects AS project
SET linear_sync_enabled = TRUE
WHERE EXISTS (
    SELECT 1
    FROM cron_jobs AS job
    WHERE job.project_id = project.id
      AND job.name = 'gobby:linear-sync:' || project.id::TEXT
      AND job.enabled = TRUE
);

ALTER TABLE project_github_triage_configs
    ADD COLUMN IF NOT EXISTS sync_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE project_github_triage_configs
    ADD COLUMN IF NOT EXISTS triage_enabled BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'project_github_triage_configs'
          AND column_name = 'enabled'
    ) THEN
        UPDATE project_github_triage_configs
        SET sync_enabled = enabled,
            triage_enabled = enabled;

        ALTER TABLE project_github_triage_configs DROP COLUMN enabled;
    END IF;
END
$$;

UPDATE cron_jobs
SET enabled = FALSE,
    next_run_at = NULL,
    updated_at = NOW()
WHERE (
      name LIKE 'gobby:linear-sync:%'
      OR name LIKE 'gobby:github-triage:%'
  );

CREATE TABLE IF NOT EXISTS external_issue_sync_status (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    provider TEXT NOT NULL CHECK (provider IN ('linear', 'github')),
    state TEXT NOT NULL DEFAULT 'disabled'
        CHECK (state IN (
            'disabled', 'pending', 'running', 'healthy', 'degraded', 'rate_limited', 'unready'
        )),
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    linked_count INTEGER NOT NULL DEFAULT 0 CHECK (linked_count >= 0),
    pending_count INTEGER NOT NULL DEFAULT 0 CHECK (pending_count >= 0),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    retry_at TIMESTAMPTZ,
    last_statistics JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_external_issue_sync_status_state
    ON external_issue_sync_status(provider, state, updated_at);
