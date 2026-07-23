CREATE TABLE IF NOT EXISTS plan_review_evidence (
    evidence_id UUID PRIMARY KEY,
    project_id UUID NOT NULL
        REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    plan_path TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    section_manifest JSONB NOT NULL,
    snapshot BYTEA NOT NULL,
    round_number INTEGER NOT NULL,
    session_id UUID
        REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    task_id UUID
        REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    stage TEXT,
    dispatch_run_id UUID
        REFERENCES agent_runs(id) ON DELETE RESTRICT DEFERRABLE INITIALLY IMMEDIATE,
    lease_expires_at TIMESTAMPTZ,
    finalized_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ,
    round_result JSONB,
    approval_result JSONB,
    approved_at TIMESTAMPTZ,
    lesson_mint_status TEXT,
    lesson_mint_detail JSONB,
    manifest_digest TEXT,
    manifest_payload JSONB,
    manifest_state TEXT,
    manifest_result JSONB,
    manifest_applied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT plan_review_evidence_round_positive CHECK (round_number > 0),
    CONSTRAINT plan_review_evidence_manifest_array
        CHECK (jsonb_typeof(section_manifest) = 'array'),
    CONSTRAINT plan_review_evidence_lifecycle_exclusive
        CHECK (NOT (finalized_at IS NOT NULL AND expired_at IS NOT NULL)),
    CONSTRAINT plan_review_evidence_attempt_binding CHECK (
        (session_id IS NOT NULL AND task_id IS NULL AND stage IS NULL)
        OR (session_id IS NULL AND task_id IS NOT NULL AND stage IS NOT NULL)
    ),
    CONSTRAINT plan_review_evidence_bound_lease_cleared
        CHECK (dispatch_run_id IS NULL OR lease_expires_at IS NULL),
    CONSTRAINT plan_review_evidence_mint_status CHECK (
        lesson_mint_status IS NULL
        OR lesson_mint_status IN ('pending', 'minted', 'failed', 'none')
    ),
    CONSTRAINT plan_review_evidence_manifest_state CHECK (
        manifest_state IS NULL
        OR manifest_state IN ('pending', 'applied', 'revoked')
    )
);

CREATE INDEX IF NOT EXISTS idx_plan_review_evidence_project_path
    ON plan_review_evidence(project_id, plan_path, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_review_evidence_active_path
    ON plan_review_evidence(project_id, plan_path)
    WHERE finalized_at IS NULL AND expired_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_review_evidence_interactive_round
ON plan_review_evidence(session_id, plan_path, round_number)
WHERE session_id IS NOT NULL AND expired_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_review_evidence_stage_round
ON plan_review_evidence(task_id, stage, round_number)
WHERE task_id IS NOT NULL AND expired_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_review_evidence_dispatch_run
    ON plan_review_evidence(dispatch_run_id)
    WHERE dispatch_run_id IS NOT NULL;
