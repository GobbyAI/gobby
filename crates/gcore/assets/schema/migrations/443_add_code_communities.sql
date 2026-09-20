ALTER TABLE code_indexed_project_states
    ADD COLUMN community_id_watermark integer NOT NULL DEFAULT 0,
    ADD COLUMN partition_signature text;

CREATE TABLE code_communities (
    machine_id uuid NOT NULL REFERENCES machines(id),
    project_id uuid NOT NULL REFERENCES code_indexed_projects(id) ON DELETE CASCADE,
    community_id integer NOT NULL,
    member_count integer NOT NULL,
    members text[] NOT NULL,
    representatives text[] NOT NULL,
    internal_edges integer NOT NULL,
    cohesion double precision NOT NULL,
    boundary jsonb NOT NULL,
    member_signature text NOT NULL,
    label_deterministic text NOT NULL,
    label text NOT NULL,
    label_source text NOT NULL,
    label_confidence double precision,
    label_model text,
    label_candidates text[] NOT NULL,
    labeled_signature text,
    labeled_at timestamp with time zone,
    label_attempted_at timestamp with time zone,
    refreshed_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (machine_id, project_id, community_id),
    CHECK (label_source IN ('deterministic', 'model')),
    CHECK (member_count = cardinality(members) AND member_count > 0),
    CHECK (member_signature ~ '^[0-9a-f]{16}$'),
    CHECK (cohesion >= 0 AND cohesion <= 1),
    CHECK (jsonb_typeof(boundary) = 'array'),
    CHECK (
        label_source = 'deterministic'
        OR (labeled_signature IS NOT NULL AND label_model IS NOT NULL)
    )
);

CREATE INDEX idx_cc_label_queue
    ON code_communities (machine_id, project_id)
    WHERE labeled_signature IS DISTINCT FROM member_signature;

ALTER TABLE ONLY code_communities FORCE ROW LEVEL SECURITY;
ALTER TABLE code_communities ENABLE ROW LEVEL SECURITY;

CREATE POLICY gobby_daemon_runtime_access
    ON code_communities
    TO gobby_daemon_runtime
    USING (true)
    WITH CHECK (true);

CREATE POLICY gobby_gcode_project_delete
    ON public.code_communities
    FOR DELETE
    TO gobby_gcode_capability
    USING (
        project_id = COALESCE(
            gobby_agent_auth.current_code_overlay_project_id(),
            gobby_agent_auth.current_project_id()
        )
        AND machine_id = gobby_agent_auth.current_machine_id()
    );

CREATE POLICY gobby_gcode_project_insert
    ON public.code_communities
    FOR INSERT
    TO gobby_gcode_capability
    WITH CHECK (
        project_id = COALESCE(
            gobby_agent_auth.current_code_overlay_project_id(),
            gobby_agent_auth.current_project_id()
        )
        AND machine_id = gobby_agent_auth.current_machine_id()
    );

CREATE POLICY gobby_gcode_project_read
    ON public.code_communities
    FOR SELECT
    TO gobby_gcode_capability
    USING (
        (
            project_id = gobby_agent_auth.current_project_id()
            OR project_id = gobby_agent_auth.current_code_overlay_project_id()
        )
        AND machine_id = gobby_agent_auth.current_machine_id()
    );

CREATE POLICY gobby_gcode_project_update
    ON public.code_communities
    FOR UPDATE
    TO gobby_gcode_capability
    USING (
        project_id = COALESCE(
            gobby_agent_auth.current_code_overlay_project_id(),
            gobby_agent_auth.current_project_id()
        )
        AND machine_id = gobby_agent_auth.current_machine_id()
    )
    WITH CHECK (
        project_id = COALESCE(
            gobby_agent_auth.current_code_overlay_project_id(),
            gobby_agent_auth.current_project_id()
        )
        AND machine_id = gobby_agent_auth.current_machine_id()
    );

CREATE POLICY gobby_migration_owner_access
    ON code_communities
    TO CURRENT_USER
    USING (true)
    WITH CHECK (true);

GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE code_communities TO gobby_daemon_runtime;
GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE code_communities TO gobby_gcode_capability;
