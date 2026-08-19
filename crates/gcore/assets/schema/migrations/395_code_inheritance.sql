-- Explicit inheritance facts for class-hierarchy graph walks.
-- Derived → base. LocalImport carriers stay retryable until promotion.

CREATE TABLE IF NOT EXISTS code_inheritance (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    source_symbol_id uuid,
    source_name text NOT NULL,
    source_kind text DEFAULT 'symbol'::text NOT NULL,
    source_external_module text DEFAULT ''::text NOT NULL,
    target_symbol_id uuid,
    target_name text NOT NULL,
    target_kind text DEFAULT 'unresolved'::text NOT NULL,
    target_external_module text DEFAULT ''::text NOT NULL,
    heritage_kind text NOT NULL,
    file_path text NOT NULL,
    content_hash text NOT NULL,
    line integer DEFAULT 0 NOT NULL
);

ALTER TABLE ONLY code_inheritance FORCE ROW LEVEL SECURITY;

DO $identity$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_attribute
        WHERE attrelid = 'code_inheritance'::regclass
          AND attname = 'id'
          AND attidentity <> ''
    ) THEN
        ALTER TABLE code_inheritance ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
            SEQUENCE NAME code_inheritance_id_seq
            START WITH 1
            INCREMENT BY 1
            NO MINVALUE
            NO MAXVALUE
            CACHE 1
        );
    END IF;
END
$identity$;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'code_inheritance_pkey'
    ) THEN
        ALTER TABLE ONLY code_inheritance
            ADD CONSTRAINT code_inheritance_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'code_inheritance_unique_target'
    ) THEN
        ALTER TABLE ONLY code_inheritance
            ADD CONSTRAINT code_inheritance_unique_target UNIQUE NULLS NOT DISTINCT (
                project_id, file_path, content_hash,
                source_symbol_id, source_name, source_kind, source_external_module,
                target_symbol_id, target_name, target_kind, target_external_module,
                heritage_kind, line
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'code_inheritance_content_fkey'
    ) THEN
        ALTER TABLE ONLY code_inheritance
            ADD CONSTRAINT code_inheritance_content_fkey
            FOREIGN KEY (project_id, file_path, content_hash)
            REFERENCES code_indexed_files(project_id, file_path, content_hash)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'code_inheritance_heritage_kind_check'
    ) THEN
        ALTER TABLE ONLY code_inheritance
            ADD CONSTRAINT code_inheritance_heritage_kind_check
            CHECK (heritage_kind IN ('INHERITS', 'EXTENDS', 'IMPLEMENTS'));
    END IF;
END
$constraints$;

CREATE INDEX IF NOT EXISTS idx_cinherit_source
    ON code_inheritance USING btree (project_id, source_symbol_id);
CREATE INDEX IF NOT EXISTS idx_cinherit_file
    ON code_inheritance USING btree (project_id, file_path);
CREATE INDEX IF NOT EXISTS idx_cinherit_target
    ON code_inheritance USING btree (project_id, target_kind, target_symbol_id, target_name);

ALTER TABLE code_inheritance ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS gobby_daemon_runtime_access ON code_inheritance;
CREATE POLICY gobby_daemon_runtime_access ON code_inheritance
    TO gobby_daemon_runtime USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS gobby_migration_owner_access ON code_inheritance;
CREATE POLICY gobby_migration_owner_access ON code_inheritance
    TO CURRENT_USER USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE code_inheritance TO gobby_daemon_runtime;
GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE code_inheritance TO gobby_gcode_capability;
GRANT ALL ON SEQUENCE code_inheritance_id_seq TO gobby_daemon_runtime;
GRANT SELECT, USAGE ON SEQUENCE code_inheritance_id_seq TO gobby_gcode_capability;

DO $gcode_inheritance_rls$
DECLARE
    target_schema NAME := current_schema();
    read_expression TEXT;
    write_expression TEXT;
BEGIN
    IF to_regclass(format('%I.%I', target_schema, 'code_inheritance')) IS NULL THEN
        RAISE EXCEPTION 'gcode authorization requires relation %.code_inheritance',
            target_schema;
    END IF;
    read_expression :=
        '(project_id = gobby_agent_auth.current_project_id() OR '
        'project_id = gobby_agent_auth.current_code_overlay_project_id())';
    write_expression :=
        'project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), '
        'gobby_agent_auth.current_project_id())';
    EXECUTE format(
        'DROP POLICY IF EXISTS gobby_gcode_project_read ON %I.code_inheritance',
        target_schema
    );
    EXECUTE format(
        'DROP POLICY IF EXISTS gobby_gcode_project_insert ON %I.code_inheritance',
        target_schema
    );
    EXECUTE format(
        'DROP POLICY IF EXISTS gobby_gcode_project_update ON %I.code_inheritance',
        target_schema
    );
    EXECUTE format(
        'DROP POLICY IF EXISTS gobby_gcode_project_delete ON %I.code_inheritance',
        target_schema
    );
    EXECUTE format(
        'CREATE POLICY gobby_gcode_project_read ON %I.code_inheritance '
        'FOR SELECT TO %I USING (%s)',
        target_schema,
        'gobby_gcode_capability',
        read_expression
    );
    EXECUTE format(
        'CREATE POLICY gobby_gcode_project_insert ON %I.code_inheritance '
        'FOR INSERT TO %I WITH CHECK (%s)',
        target_schema,
        'gobby_gcode_capability',
        write_expression
    );
    EXECUTE format(
        'CREATE POLICY gobby_gcode_project_update ON %I.code_inheritance '
        'FOR UPDATE TO %I USING (%s) WITH CHECK (%s)',
        target_schema,
        'gobby_gcode_capability',
        write_expression,
        write_expression
    );
    EXECUTE format(
        'CREATE POLICY gobby_gcode_project_delete ON %I.code_inheritance '
        'FOR DELETE TO %I USING (%s)',
        target_schema,
        'gobby_gcode_capability',
        write_expression
    );
END
$gcode_inheritance_rls$;
