-- Task criteria are optional metadata; selected lifecycle policy decides requirements.
ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria;

-- Closure reasons are policy data. Keep only bounded, nonblank metadata integrity.
ALTER TABLE tasks DROP CONSTRAINT tasks_closed_reason_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_closed_reason_check CHECK (
    closed_at IS NULL OR (
        closed_reason IS NOT NULL AND btrim(closed_reason) <> '' AND length(closed_reason) <= 120
    )
);
