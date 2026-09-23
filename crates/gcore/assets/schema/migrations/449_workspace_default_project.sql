ALTER TABLE workspaces
    ADD COLUMN default_project_id uuid REFERENCES projects (id) ON DELETE SET NULL;

CREATE UNIQUE INDEX idx_workspaces_machine_default_project
    ON workspaces (machine_id, default_project_id)
    WHERE default_project_id IS NOT NULL;
