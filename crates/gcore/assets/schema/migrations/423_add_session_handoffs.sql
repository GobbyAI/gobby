CREATE TABLE session_handoffs (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    payload_version smallint DEFAULT 1 NOT NULL,

    current_state text NOT NULL,
    next_steps_json jsonb NOT NULL,
    what_was_accomplished_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    key_decisions_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    problems_encountered_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    what_didnt_work_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    blockers_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    notes_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    references_json jsonb DEFAULT '[]'::jsonb NOT NULL,

    rendered_markdown text NOT NULL,
    content_sha256 text NOT NULL,
    authored_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT session_handoffs_pkey PRIMARY KEY (id),
    CONSTRAINT session_handoffs_session_id_fkey
        FOREIGN KEY (session_id)
        REFERENCES sessions(id)
        ON DELETE CASCADE
        DEFERRABLE,
    CONSTRAINT session_handoffs_payload_version_valid
        CHECK (payload_version = 1),
    CONSTRAINT session_handoffs_current_state_nonblank
        CHECK (btrim(current_state) <> ''),
    CONSTRAINT session_handoffs_next_steps_array
        CHECK (
            jsonb_typeof(next_steps_json) = 'array'
            AND jsonb_array_length(next_steps_json) > 0
        ),
    CONSTRAINT session_handoffs_what_was_accomplished_array
        CHECK (jsonb_typeof(what_was_accomplished_json) = 'array'),
    CONSTRAINT session_handoffs_key_decisions_array
        CHECK (jsonb_typeof(key_decisions_json) = 'array'),
    CONSTRAINT session_handoffs_problems_encountered_array
        CHECK (jsonb_typeof(problems_encountered_json) = 'array'),
    CONSTRAINT session_handoffs_what_didnt_work_array
        CHECK (jsonb_typeof(what_didnt_work_json) = 'array'),
    CONSTRAINT session_handoffs_blockers_array
        CHECK (jsonb_typeof(blockers_json) = 'array'),
    CONSTRAINT session_handoffs_notes_array
        CHECK (jsonb_typeof(notes_json) = 'array'),
    CONSTRAINT session_handoffs_references_array
        CHECK (jsonb_typeof(references_json) = 'array'),
    CONSTRAINT session_handoffs_rendered_markdown_nonblank
        CHECK (btrim(rendered_markdown) <> ''),
    CONSTRAINT session_handoffs_content_sha256_valid
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX idx_session_handoffs_session_authored
    ON session_handoffs (session_id, authored_at DESC, id DESC);

CREATE TABLE session_handoff_deliveries (
    handoff_id uuid NOT NULL,
    attempt_id text NOT NULL,
    boundary_kind text NOT NULL,
    continuation_session_id uuid NOT NULL,
    delivered_at timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT session_handoff_deliveries_pkey PRIMARY KEY (handoff_id),
    CONSTRAINT session_handoff_deliveries_attempt_id_key UNIQUE (attempt_id),
    CONSTRAINT session_handoff_deliveries_handoff_id_fkey
        FOREIGN KEY (handoff_id)
        REFERENCES session_handoffs(id)
        ON DELETE CASCADE
        DEFERRABLE,
    CONSTRAINT session_handoff_deliveries_attempt_id_valid
        CHECK (attempt_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT session_handoff_deliveries_boundary_kind_valid
        CHECK (boundary_kind = ANY (ARRAY['compact'::text, 'clear'::text]))
);

CREATE INDEX idx_session_handoff_deliveries_continuation
    ON session_handoff_deliveries (continuation_session_id, delivered_at DESC);

GRANT SELECT, INSERT, DELETE
    ON TABLE session_handoffs
    TO gobby_daemon_runtime;

GRANT SELECT, INSERT
    ON TABLE session_handoff_deliveries
    TO gobby_daemon_runtime;
