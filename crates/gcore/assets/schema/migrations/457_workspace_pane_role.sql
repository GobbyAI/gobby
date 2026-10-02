-- Pane roles are opaque runbook metadata; NULL denotes an ordinary run.
ALTER TABLE workspace_panes
    ADD COLUMN role text,
    ADD CONSTRAINT workspace_panes_role_byte_limit
        CHECK (role IS NULL OR octet_length(role) BETWEEN 1 AND 1024);
