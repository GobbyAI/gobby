ALTER TABLE task_close_reviews
DROP CONSTRAINT task_close_reviews_status_check;

ALTER TABLE task_close_reviews
ADD CONSTRAINT task_close_reviews_status_check
CHECK (
    status = ANY (
        ARRAY[
            'queued'::text,
            'launching'::text,
            'running'::text,
            'finalizing'::text,
            'closed'::text,
            'invalid'::text,
            'external_pending'::text,
            'stale'::text,
            'error'::text
        ]
    )
);

DROP INDEX uq_task_close_reviews_active_task;

CREATE UNIQUE INDEX uq_task_close_reviews_active_task
ON task_close_reviews USING btree (task_id)
WHERE status = ANY (
    ARRAY[
        'queued'::text,
        'launching'::text,
        'running'::text,
        'finalizing'::text
    ]
);
