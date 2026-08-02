-- Migration 342 added tasks_require_validation_criteria as NOT VALID precisely
-- because rows predating the requirement carry a NULL validation_criteria.
-- Retyping a legacy review_anchor row re-checks that constraint for the updated
-- row, so the UPDATE below would abort on those grandfathered rows. Drop the
-- constraint and re-add it in its original NOT VALID form around the update so
-- history is preserved verbatim rather than rejected or given fabricated
-- criteria. DDL holds ACCESS EXCLUSIVE on tasks for the whole migration
-- transaction, so no concurrent write can slip through the gap.
ALTER TABLE tasks
DROP CONSTRAINT IF EXISTS tasks_require_validation_criteria;

UPDATE tasks
SET task_type = 'task'
WHERE task_type = 'review_anchor';

ALTER TABLE tasks
ADD CONSTRAINT tasks_require_validation_criteria
CHECK (
    task_type = 'epic'
    OR NULLIF(BTRIM(validation_criteria), '') IS NOT NULL
)
NOT VALID;

DELETE FROM task_type_default_stages
WHERE task_type = 'review_anchor';
