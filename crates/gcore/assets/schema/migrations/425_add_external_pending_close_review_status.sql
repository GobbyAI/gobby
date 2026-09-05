ALTER TABLE task_close_reviews
    DROP CONSTRAINT task_close_reviews_status_check;

ALTER TABLE task_close_reviews
    ADD CONSTRAINT task_close_reviews_status_check
        CHECK (status = ANY (ARRAY[
            'launching'::text,
            'running'::text,
            'finalizing'::text,
            'closed'::text,
            'invalid'::text,
            'external_pending'::text,
            'stale'::text,
            'error'::text
        ]));
