-- Canonical workspace identity for ACP close/delete confinement.
-- workspace_path is the resolved project or worktree path recorded at first
-- workspace resolution. A NULL path is absent or tombstoned. workspace_generation
-- is incremented by project switch, worktree switch, and worktree deletion in
-- the same transaction as the identity change.

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS workspace_path text,
    ADD COLUMN IF NOT EXISTS workspace_generation integer DEFAULT 0 NOT NULL;

ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_workspace_generation_nonnegative;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_workspace_generation_nonnegative
    CHECK (workspace_generation >= 0);
