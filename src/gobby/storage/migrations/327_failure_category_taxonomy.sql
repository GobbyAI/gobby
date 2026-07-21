ALTER TABLE task_lifecycle_events
ADD COLUMN IF NOT EXISTS failure_category TEXT CHECK (
    failure_category IN ('environment', 'dependency', 'code', 'test', 'provider', 'timeout')
);

ALTER TABLE task_validation_history
ADD COLUMN IF NOT EXISTS failure_category TEXT CHECK (
    failure_category IN ('environment', 'dependency', 'code', 'test', 'provider', 'timeout')
);
