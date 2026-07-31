-- gobby:destructive
-- Adopt the dream truth table previously created by runtime DDL.
CREATE TABLE IF NOT EXISTS memory_dream_truth_state (
    project_id TEXT PRIMARY KEY,
    digest_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Align live columns created by older runtime/migration shapes with the
-- canonical baseline definitions.
ALTER TABLE memories
    ALTER COLUMN memory_type SET DEFAULT 'fact',
    ALTER COLUMN dream_due_version TYPE BIGINT
        USING dream_due_version::BIGINT;

ALTER TABLE tool_results
    ALTER COLUMN total_chars TYPE BIGINT
        USING total_chars::BIGINT;

-- These legacy columns are absent from the canonical schema and have no
-- production readers. Their remaining live values are historical state.
DROP INDEX IF EXISTS idx_inter_session_messages_unread;

ALTER TABLE inter_session_messages
    DROP COLUMN IF EXISTS read_at;

ALTER TABLE tasks
    DROP COLUMN IF EXISTS assignee;

-- ADD CONSTRAINT lacks IF NOT EXISTS, while fresh schemas already contain
-- these baseline constraints before replaying migration 356.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'cron_jobs'::regclass
          AND conname = 'cron_jobs_name_key'
    ) THEN
        ALTER TABLE cron_jobs
            ADD CONSTRAINT cron_jobs_name_key UNIQUE (name);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'projects'::regclass
          AND conname = 'projects_linear_sync_enabled_check'
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_linear_sync_enabled_check
            CHECK (linear_sync_enabled IN (FALSE, TRUE));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'project_github_triage_configs'::regclass
          AND conname = 'project_github_triage_configs_sync_enabled_check'
    ) THEN
        ALTER TABLE project_github_triage_configs
            ADD CONSTRAINT project_github_triage_configs_sync_enabled_check
            CHECK (sync_enabled IN (FALSE, TRUE));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'project_github_triage_configs'::regclass
          AND conname = 'project_github_triage_configs_triage_enabled_check'
    ) THEN
        ALTER TABLE project_github_triage_configs
            ADD CONSTRAINT project_github_triage_configs_triage_enabled_check
            CHECK (triage_enabled IN (FALSE, TRUE));
    END IF;
END
$$;
