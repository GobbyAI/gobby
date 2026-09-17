-- Node refs (`n#`). Existing rows stay NULL until the allocator assigns one on
-- their next upsert_seen.
ALTER TABLE machines ADD COLUMN ref integer;
CREATE UNIQUE INDEX idx_machines_owner_ref ON machines (owner_user_id, ref)
    WHERE ref IS NOT NULL;

-- Node-owned workspaces (`w#`). Focus columns are seeds for the next window.
CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    machine_id uuid NOT NULL REFERENCES machines (id),
    ref integer NOT NULL,
    name text NOT NULL,
    focused_project_id uuid REFERENCES projects (id) ON DELETE SET NULL DEFERRABLE,
    focused_tab_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (machine_id, ref),
    UNIQUE (machine_id, name)
);

-- Tabs (`t#`). layout is the split tree: {"kind":"pane","pane_id":...} leaves and
-- {"kind":"split","axis":"horizontal|vertical","ratio":<f32>,"children":[...]} nodes.
CREATE TABLE workspace_tabs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    ref integer NOT NULL,
    title text,
    project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    worktree_id uuid REFERENCES worktrees (id) ON DELETE SET NULL DEFERRABLE,
    position integer NOT NULL,
    focused_pane_id uuid,
    layout jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, ref)
);

-- Panes (`p#`). terminal_id is NULL while a spawn is in flight and after the
-- terminal row is removed; owns_terminal is false for adopted terminals.
CREATE TABLE workspace_panes (
    id uuid PRIMARY KEY,
    tab_id uuid NOT NULL REFERENCES workspace_tabs (id) ON DELETE CASCADE,
    ref integer NOT NULL,
    terminal_id uuid REFERENCES terminals (id) ON DELETE SET NULL DEFERRABLE,
    owns_terminal boolean NOT NULL,
    label text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workspace_panes_label_byte_limit
        CHECK (label IS NULL OR octet_length(label) <= 1024),
    UNIQUE (tab_id, ref)
);
-- One terminal sits in at most one pane, so a racing adopt fails.
CREATE UNIQUE INDEX idx_workspace_panes_terminal ON workspace_panes (terminal_id)
    WHERE terminal_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON workspaces, workspace_tabs, workspace_panes
    TO gobby_daemon_runtime;
