-- Keep the retired close-review records in a read-only historical namespace.
ALTER TABLE task_close_reviews RENAME TO task_close_review_history;
ALTER TABLE task_close_review_history
    RENAME CONSTRAINT task_close_reviews_pkey TO task_close_review_history_pkey;
ALTER TABLE task_close_review_history
    RENAME CONSTRAINT task_close_reviews_close_arguments_check
    TO task_close_review_history_close_arguments_check;
ALTER TABLE task_close_review_history
    RENAME CONSTRAINT task_close_reviews_result_payload_check
    TO task_close_review_history_result_payload_check;
ALTER TABLE task_close_review_history
    RENAME CONSTRAINT task_close_reviews_status_check TO task_close_review_history_status_check;
DROP INDEX IF EXISTS uq_task_close_reviews_active_task;
ALTER INDEX idx_task_close_reviews_recovery RENAME TO idx_task_close_review_history_status;
ALTER INDEX uq_task_close_reviews_agent_run RENAME TO uq_task_close_review_history_agent_run;
REVOKE INSERT, UPDATE, DELETE ON task_close_review_history FROM gobby_daemon_runtime;

-- Preserve every prior task-level outcome before starting the independent-review epoch.
INSERT INTO task_comments (id, task_id, author, author_type, body)
SELECT gen_random_uuid(), id, 'migration:474', 'system',
       jsonb_build_object(
           'kind', 'legacy_review_snapshot',
           'epoch', 0,
           'status', validation_status,
           'feedback', validation_feedback,
           'fail_count', validation_fail_count,
           'closed_at', closed_at,
           'closed_commit_sha', closed_commit_sha,
           'updated_at', updated_at
       )::text
FROM tasks
WHERE validation_status IS NOT NULL
   OR validation_feedback IS NOT NULL
   OR COALESCE(validation_fail_count, 0) <> 0;

ALTER TABLE tasks RENAME COLUMN validation_status TO review_status;
ALTER TABLE tasks RENAME COLUMN validation_feedback TO review_feedback;
ALTER TABLE tasks RENAME COLUMN validation_fail_count TO review_fail_count;
ALTER TABLE tasks RENAME CONSTRAINT tasks_validation_status_check TO tasks_review_status_check;
ALTER TABLE tasks ADD COLUMN review_epoch integer NOT NULL DEFAULT 1
    CONSTRAINT tasks_review_epoch_check CHECK (review_epoch > 0);

UPDATE tasks SET review_status = 'pending', review_feedback = NULL, review_fail_count = 0
WHERE closed_at IS NULL;
UPDATE tasks SET review_status = 'pending' WHERE review_status IS NULL;
UPDATE tasks SET review_fail_count = 0 WHERE review_fail_count IS NULL;
ALTER TABLE tasks ALTER COLUMN review_status SET DEFAULT 'pending';
ALTER TABLE tasks ALTER COLUMN review_status SET NOT NULL;
ALTER TABLE tasks ALTER COLUMN review_fail_count SET NOT NULL;
ALTER TABLE tasks ADD CONSTRAINT tasks_review_fail_count_check CHECK (review_fail_count >= 0);
