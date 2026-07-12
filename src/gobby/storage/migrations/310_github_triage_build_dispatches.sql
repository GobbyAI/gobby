-- Durable successful-build markers keep GitHub triage retries honest (#17989).
CREATE TABLE IF NOT EXISTS gh_triage_build_dispatches (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    dispatched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, repo, issue_number)
);

INSERT INTO gh_triage_build_dispatches (project_id, repo, issue_number, task_id, dispatched_at)
SELECT project_id, repo, issue_number, task_id, last_triaged_at
FROM gh_issues_triaged
WHERE verdict = 'implement' AND task_id IS NOT NULL
ON CONFLICT (project_id, repo, issue_number) DO NOTHING;
