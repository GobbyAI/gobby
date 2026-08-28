-- 408: terminals table and agent_runs.terminal_id (herdr foundation, plan 1.2).
--
-- The TerminalRuntime seam persists every Gobby-owned or discovered terminal as
-- a `terminals` row and links agent runs to it through `agent_runs.terminal_id`.
-- `agent_runs.tmux_session_name` has no reader once runs carry a terminal id; it
-- is dropped so a migrated hub matches the catalog a fresh apply produces.
-- Fresh lineages execute this file after the baseline, so every statement that
-- can meet an existing object is guarded.

CREATE TABLE IF NOT EXISTS terminals (
    id uuid NOT NULL,
    backend text NOT NULL,
    ownership text NOT NULL,
    state text NOT NULL,
    spawn_key text,
    machine_id uuid NOT NULL,
    locator jsonb,
    locator_key text,
    session_name text,
    window_id text,
    title text,
    host_epoch text,
    unresolved_writes jsonb DEFAULT '{}'::jsonb NOT NULL,
    automatic_write_quarantined_at timestamp with time zone,
    automatic_write_quarantine_action_key text,
    attempt_generation integer DEFAULT 1 NOT NULL,
    attempt_started_at timestamp with time zone DEFAULT now() NOT NULL,
    process jsonb,
    rows integer,
    cols integer,
    project_id uuid NOT NULL,
    session_id uuid,
    agent_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    liveness_at timestamp with time zone,
    CONSTRAINT terminals_pkey PRIMARY KEY (id),
    CONSTRAINT terminals_backend_check CHECK ((backend = ANY (ARRAY['tmux'::text, 'native'::text]))),
    CONSTRAINT terminals_ownership_check CHECK ((ownership = ANY (ARRAY['gobby'::text, 'external'::text]))),
    CONSTRAINT terminals_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'live'::text, 'exited'::text, 'orphaned'::text]))),
    CONSTRAINT terminals_title_byte_limit CHECK ((title IS NULL OR octet_length(title) <= 1024)),
    CONSTRAINT terminals_locator_present_when_attachable CHECK (
        (state = ANY (ARRAY['pending'::text, 'exited'::text]))
        OR ((locator IS NOT NULL) AND (locator_key IS NOT NULL))
    ),
    CONSTRAINT terminals_locator_pair_consistent CHECK ((locator IS NULL) = (locator_key IS NULL)),
    CONSTRAINT terminals_spawn_key_matches_ownership CHECK (
        ((ownership = 'gobby'::text) AND (spawn_key IS NOT NULL))
        OR ((ownership = 'external'::text) AND (spawn_key IS NULL))
    ),
    CONSTRAINT terminals_external_is_never_pending CHECK (
        (ownership = 'gobby'::text) OR (state <> 'pending'::text)
    ),
    CONSTRAINT terminals_host_epoch_is_native_only CHECK (
        (host_epoch IS NULL) OR (backend = 'native'::text)
    ),
    CONSTRAINT terminals_native_attachable_has_epoch CHECK (
        (backend <> 'native'::text)
        OR (state <> ALL (ARRAY['live'::text, 'orphaned'::text]))
        OR (host_epoch IS NOT NULL)
    ),
    CONSTRAINT terminals_process_is_native_only CHECK (
        (process IS NULL) OR (backend = 'native'::text)
    ),
    CONSTRAINT terminals_pending_has_no_identity CHECK (
        (state <> 'pending'::text)
        OR ((locator IS NULL) AND (locator_key IS NULL) AND (host_epoch IS NULL))
    ),
    CONSTRAINT terminals_external_always_has_locator CHECK (
        (ownership = 'gobby'::text)
        OR ((locator IS NOT NULL) AND (locator_key IS NOT NULL))
    ),
    CONSTRAINT terminals_quarantine_pair_consistent CHECK (
        (automatic_write_quarantined_at IS NULL)
        = (automatic_write_quarantine_action_key IS NULL)
    ),
    CONSTRAINT terminals_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES machines(id),
    CONSTRAINT terminals_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects(id),
    CONSTRAINT terminals_session_id_fkey FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE,
    CONSTRAINT terminals_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL DEFERRABLE
);

CREATE INDEX IF NOT EXISTS idx_terminals_run ON terminals USING btree (agent_run_id) WHERE (agent_run_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_terminals_machine ON terminals USING btree (machine_id);

CREATE INDEX IF NOT EXISTS idx_terminals_project ON terminals USING btree (project_id);

CREATE INDEX IF NOT EXISTS idx_terminals_live ON terminals USING btree (state) WHERE (state = ANY (ARRAY['pending'::text, 'live'::text]));

CREATE UNIQUE INDEX IF NOT EXISTS idx_terminals_locator_key_active ON terminals USING btree (locator_key) WHERE ((locator_key IS NOT NULL) AND (state = ANY (ARRAY['pending'::text, 'live'::text])));

CREATE UNIQUE INDEX IF NOT EXISTS idx_terminals_spawn_key ON terminals USING btree (spawn_key) WHERE (spawn_key IS NOT NULL);

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS terminal_id uuid;

ALTER TABLE agent_runs DROP COLUMN IF EXISTS tmux_session_name;

ALTER TABLE ONLY agent_runs
    ADD CONSTRAINT agent_runs_terminal_id_fkey FOREIGN KEY (terminal_id) REFERENCES terminals(id);

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE terminals TO gobby_daemon_runtime;
