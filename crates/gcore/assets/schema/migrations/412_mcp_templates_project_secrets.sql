-- Bundled MCP server templates, instance provenance, project-scoped secrets.
CREATE TABLE mcp_server_templates (
    id uuid NOT NULL,
    name text NOT NULL,
    project_id uuid NOT NULL,
    owner text NOT NULL DEFAULT 'user',        -- 'gobby' | 'user'
    source_path text,
    definition jsonb NOT NULL,
    definition_hash text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);
ALTER TABLE ONLY mcp_server_templates ADD CONSTRAINT mcp_server_templates_pkey PRIMARY KEY (id);
ALTER TABLE ONLY mcp_server_templates ADD CONSTRAINT mcp_server_templates_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX idx_mcp_server_templates_name_project ON mcp_server_templates (name, project_id);
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE mcp_server_templates TO gobby_daemon_runtime;

ALTER TABLE mcp_servers ADD COLUMN template_id uuid;
ALTER TABLE mcp_servers ADD COLUMN template_values jsonb;
ALTER TABLE mcp_servers ADD COLUMN runtime_hook text;
ALTER TABLE ONLY mcp_servers ADD CONSTRAINT mcp_servers_template_id_fkey
    FOREIGN KEY (template_id) REFERENCES mcp_server_templates(id) ON DELETE SET NULL;
CREATE INDEX idx_mcp_servers_template_id ON mcp_servers (template_id);

ALTER TABLE secrets ADD COLUMN project_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000002';
ALTER TABLE ONLY secrets ADD CONSTRAINT secrets_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY secrets DROP CONSTRAINT secrets_name_key;
CREATE UNIQUE INDEX idx_secrets_name_project ON secrets (name, project_id);
