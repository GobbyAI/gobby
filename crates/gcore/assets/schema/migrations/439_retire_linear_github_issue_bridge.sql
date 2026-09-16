-- Retire Linear/GitHub Gobby issue/PR identity (#22365, #22385).
-- Executes on fresh lineages immediately after the canonical baseline and on
-- installed hubs at the next apply. Every statement is IF EXISTS so both
-- converge on the same catalog.
-- Qualify every target to the runner's schema: IF EXISTS must never fall through
-- search_path into another schema. RESTRICT refuses unrecorded outside consumers.
DO $retire_linear_github$
BEGIN
    EXECUTE format(
        'DROP TABLE IF EXISTS %1$I.task_delivery_units, %1$I.task_delivery_campaigns, '
        '%1$I.gh_triage_deliveries, %1$I.gh_triage_build_dispatches, '
        '%1$I.gh_issues_triaged, %1$I.project_github_triage_configs, '
        '%1$I.external_issue_sync_status RESTRICT',
        current_schema()
    );
    EXECUTE format(
        'DROP INDEX IF EXISTS %1$I.idx_tasks_github_issue_link',
        current_schema()
    );
    EXECUTE format(
        'ALTER TABLE %1$I.projects DROP CONSTRAINT IF EXISTS projects_linear_sync_enabled_check',
        current_schema()
    );
    EXECUTE format(
        'ALTER TABLE %1$I.projects '
        'DROP COLUMN IF EXISTS github_url, '
        'DROP COLUMN IF EXISTS github_repo, '
        'DROP COLUMN IF EXISTS linear_team_id, '
        'DROP COLUMN IF EXISTS linear_project_id, '
        'DROP COLUMN IF EXISTS linear_synced_at, '
        'DROP COLUMN IF EXISTS linear_sync_enabled',
        current_schema()
    );
    EXECUTE format(
        'ALTER TABLE %1$I.tasks '
        'DROP COLUMN IF EXISTS github_issue_number, '
        'DROP COLUMN IF EXISTS github_pr_number, '
        'DROP COLUMN IF EXISTS github_repo, '
        'DROP COLUMN IF EXISTS linear_issue_id, '
        'DROP COLUMN IF EXISTS linear_team_id',
        current_schema()
    );
END
$retire_linear_github$;
