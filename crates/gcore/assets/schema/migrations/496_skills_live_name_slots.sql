-- A soft-deleted skill keeps its row but no longer holds its (name, project, source) slot,
-- so a same-name install or move into that scope can reuse the slot.
ALTER TABLE skills DROP CONSTRAINT idx_skills_name_project_source;
CREATE UNIQUE INDEX idx_skills_name_project_source
    ON skills (name, project_id, source) NULLS NOT DISTINCT
    WHERE deleted_at IS NULL;
