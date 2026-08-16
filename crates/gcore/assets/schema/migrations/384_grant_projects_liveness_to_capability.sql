-- Wiki writer locks run `SELECT deleted_at IS NULL FROM projects`.
-- gobby_gcode_capability had projects.repo_path (C1 forbids it) and lacked
-- deleted_at, so gwiki refresh/prune failed with config_error.
REVOKE SELECT (repo_path) ON TABLE projects FROM gobby_gcode_capability;
GRANT SELECT (deleted_at) ON TABLE projects TO gobby_gcode_capability;
