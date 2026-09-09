ALTER TABLE feedback_review_runs ADD COLUMN observations jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE feedback_review_runs DROP CONSTRAINT feedback_review_runs_status_vocab;
ALTER TABLE feedback_review_runs ADD CONSTRAINT feedback_review_runs_status_vocab
    CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'partial'));

CREATE TABLE memory_dream_decisions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES memory_dream_runs(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    proposals jsonb NOT NULL,
    effective_action jsonb NOT NULL,
    candidate jsonb,
    status text NOT NULL CHECK (status IN
        ('pending', 'planned', 'applied', 'noop', 'skipped', 'failed', 'interrupted')),
    outcome jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot_id integer REFERENCES memory_dream_snapshots(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (run_id, ordinal)
);

CREATE TABLE synthesis_reports (
    source_kind text NOT NULL CHECK (source_kind IN ('feedback', 'dream')),
    source_run_id uuid NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'synthesizing', 'publishing', 'completed', 'failed', 'interrupted')),
    task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
    worktree_id uuid REFERENCES worktrees(id) ON DELETE SET NULL,
    branch_name text,
    report_path text NOT NULL,
    content text,
    content_hash text,
    commit_sha text,
    auto_retries integer NOT NULL DEFAULT 0 CHECK (auto_retries BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_kind, source_run_id)
);

CREATE TABLE synthesis_report_attempts (
    id uuid PRIMARY KEY,
    source_kind text NOT NULL,
    source_run_id uuid NOT NULL,
    phase text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    agent_run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
    error text,
    diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    FOREIGN KEY (source_kind, source_run_id)
        REFERENCES synthesis_reports(source_kind, source_run_id) ON DELETE CASCADE
);
CREATE INDEX idx_synthesis_report_attempts_source
    ON synthesis_report_attempts(source_kind, source_run_id, started_at, id);

REVOKE ALL ON memory_dream_decisions, synthesis_reports, synthesis_report_attempts FROM PUBLIC;
