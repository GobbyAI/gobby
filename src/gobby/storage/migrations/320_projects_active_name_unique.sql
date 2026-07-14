ALTER TABLE projects
DROP CONSTRAINT IF EXISTS projects_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_active_name ON projects(name)
WHERE deleted_at IS NULL;
