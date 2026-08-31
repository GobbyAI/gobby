-- Wiki writer locks run `SELECT deleted_at IS NULL FROM projects`.
GRANT SELECT (deleted_at) ON TABLE projects TO gobby_gcode_capability;
