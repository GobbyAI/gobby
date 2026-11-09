-- Owner, next action and blocker recorded for a registered worktree. The status report
-- shows a note only while it is newer than the tree's last commit or dirty change.
CREATE TABLE worktree_notes (
    worktree_id uuid PRIMARY KEY REFERENCES worktrees (id) ON DELETE CASCADE,
    owner text NOT NULL,
    next_action text NOT NULL,
    blocker text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON worktree_notes TO gobby_daemon_runtime;
