CREATE TABLE task_close_reviews (
    id uuid NOT NULL,
    task_id uuid NOT NULL,
    task_ref text NOT NULL,
    caller_session_id uuid NOT NULL,
    agent_run_id uuid,
    close_arguments jsonb NOT NULL,
    review_fingerprint text NOT NULL,
    evidence_fingerprint text NOT NULL,
    status text DEFAULT 'launching'::text NOT NULL,
    result_payload jsonb,
    error text,
    launched_at timestamp with time zone,
    completed_at timestamp with time zone,
    delivered_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_close_reviews_pkey PRIMARY KEY (id),
    CONSTRAINT task_close_reviews_status_check CHECK (
        status = ANY (
            ARRAY[
                'launching'::text,
                'running'::text,
                'finalizing'::text,
                'closed'::text,
                'invalid'::text,
                'stale'::text,
                'error'::text
            ]
        )
    ),
    CONSTRAINT task_close_reviews_close_arguments_check CHECK (
        jsonb_typeof(close_arguments) = 'object'::text
    ),
    CONSTRAINT task_close_reviews_result_payload_check CHECK (
        result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'::text
    )
);

CREATE UNIQUE INDEX uq_task_close_reviews_active_task
ON task_close_reviews USING btree (task_id)
WHERE status = ANY (ARRAY['launching'::text, 'running'::text, 'finalizing'::text]);

CREATE UNIQUE INDEX uq_task_close_reviews_agent_run
ON task_close_reviews USING btree (agent_run_id)
WHERE agent_run_id IS NOT NULL;

CREATE INDEX idx_task_close_reviews_recovery
ON task_close_reviews USING btree (status, delivered_at, updated_at);

GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE task_close_reviews
TO gobby_daemon_runtime;
