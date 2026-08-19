use thiserror::Error;

const POSTGRES_IDENTIFIER_MAX_BYTES: usize = 63;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExternalPostgresObjectKind {
    Preflight,
    Table,
    Index,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExternalPostgresObject {
    pub name: &'static str,
    pub kind: ExternalPostgresObjectKind,
    pub sql: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{message}")]
pub struct ExternalSchemaError {
    pub object: String,
    pub message: String,
}

pub fn gcode_postgres_objects(
    schema: &str,
) -> Result<Vec<ExternalPostgresObject>, ExternalSchemaError> {
    let code_indexed_projects = qualified_relation(schema, "code_indexed_projects")?;
    let code_indexed_project_states = qualified_relation(schema, "code_indexed_project_states")?;
    let code_indexed_file_states = qualified_relation(schema, "code_indexed_file_states")?;
    let code_indexed_files = qualified_relation(schema, "code_indexed_files")?;
    let code_symbols = qualified_relation(schema, "code_symbols")?;
    let code_content_chunks = qualified_relation(schema, "code_content_chunks")?;
    let code_imports = qualified_relation(schema, "code_imports")?;
    let code_calls = qualified_relation(schema, "code_calls")?;
    let code_inheritance = qualified_relation(schema, "code_inheritance")?;

    Ok(vec![
        object(
            "pg_search extension",
            ExternalPostgresObjectKind::Preflight,
            "CREATE EXTENSION IF NOT EXISTS pg_search;".to_string(),
        ),
        object(
            "code_indexed_projects table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_indexed_projects} (
                    id UUID PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );"
            ),
        ),
        object(
            "code_indexed_project_states table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_indexed_project_states} (
                    machine_id UUID NOT NULL,
                    project_id UUID NOT NULL REFERENCES {code_indexed_projects}(id) ON DELETE CASCADE,
                    root_path TEXT NOT NULL,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    total_symbols INTEGER NOT NULL DEFAULT 0,
                    last_indexed_at TIMESTAMPTZ,
                    index_duration_ms INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (machine_id, project_id)
                );"
            ),
        ),
        object(
            "code_indexed_files table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_indexed_files} (
                    id UUID PRIMARY KEY,
                    project_id UUID NOT NULL,
                    file_path TEXT NOT NULL,
                    language TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    symbol_count INTEGER NOT NULL DEFAULT 0,
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    graph_synced BOOLEAN NOT NULL DEFAULT FALSE,
                    vectors_synced BOOLEAN NOT NULL DEFAULT FALSE,
                    graph_sync_attempted_at TIMESTAMPTZ,
                    vector_sync_attempted_at TIMESTAMPTZ,
                    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (project_id, file_path, content_hash),
                    FOREIGN KEY (project_id) REFERENCES {code_indexed_projects}(id) ON DELETE CASCADE
                );"
            ),
        ),
        object(
            "code_indexed_file_states table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_indexed_file_states} (
                    machine_id UUID NOT NULL,
                    project_id UUID NOT NULL,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (machine_id, project_id, file_path),
                    FOREIGN KEY (machine_id, project_id)
                        REFERENCES {code_indexed_project_states}(machine_id, project_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (project_id, file_path, content_hash)
                        REFERENCES {code_indexed_files}(project_id, file_path, content_hash)
                );"
            ),
        ),
        object(
            "idx_cifs_content index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cifs_content
                 ON {code_indexed_file_states}(project_id, file_path, content_hash);"
            ),
        ),
        object(
            "idx_cifs_machine_project index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cifs_machine_project
                 ON {code_indexed_file_states}(machine_id, project_id);"
            ),
        ),
        object(
            "idx_cips_project index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cips_project
                 ON {code_indexed_project_states}(project_id);"
            ),
        ),
        object(
            "idx_cif_project index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cif_project
                 ON {code_indexed_files}(project_id);"
            ),
        ),
        object(
            "idx_cif_graph_synced index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cif_graph_synced
                 ON {code_indexed_files}(project_id, graph_synced);"
            ),
        ),
        object(
            "idx_cif_vectors_synced index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cif_vectors_synced
                 ON {code_indexed_files}(project_id, vectors_synced);"
            ),
        ),
        object(
            "code_symbols table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_symbols} (
                    id UUID PRIMARY KEY,
                    project_id UUID NOT NULL,
                    file_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    language TEXT NOT NULL,
                    byte_start INTEGER NOT NULL,
                    byte_end INTEGER NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    signature TEXT,
                    docstring TEXT,
                    parent_symbol_id UUID,
                    file_content_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    summary TEXT,
                    summary_attempted_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    FOREIGN KEY (project_id, file_path, file_content_hash)
                        REFERENCES {code_indexed_files}(project_id, file_path, content_hash)
                        ON DELETE CASCADE
                );"
            ),
        ),
        object(
            "idx_cs_project index",
            ExternalPostgresObjectKind::Index,
            format!("CREATE INDEX IF NOT EXISTS idx_cs_project ON {code_symbols}(project_id);"),
        ),
        object(
            "idx_cs_file index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cs_file
                 ON {code_symbols}(project_id, file_path);"
            ),
        ),
        object(
            "idx_cs_name index",
            ExternalPostgresObjectKind::Index,
            format!("CREATE INDEX IF NOT EXISTS idx_cs_name ON {code_symbols}(name);"),
        ),
        object(
            "idx_cs_qualified index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cs_qualified
                 ON {code_symbols}(qualified_name);"
            ),
        ),
        object(
            "idx_cs_kind index",
            ExternalPostgresObjectKind::Index,
            format!("CREATE INDEX IF NOT EXISTS idx_cs_kind ON {code_symbols}(kind);"),
        ),
        object(
            "idx_cs_parent index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cs_parent
                 ON {code_symbols}(parent_symbol_id);"
            ),
        ),
        object(
            "code_content_chunks table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_content_chunks} (
                    id UUID PRIMARY KEY,
                    project_id UUID NOT NULL,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    language TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT code_content_chunks_project_file_hash_chunk_index_key
                    UNIQUE (project_id, file_path, content_hash, chunk_index),
                    FOREIGN KEY (project_id, file_path, content_hash)
                        REFERENCES {code_indexed_files}(project_id, file_path, content_hash)
                        ON DELETE CASCADE
                );"
            ),
        ),
        object(
            "idx_ccc_project index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_ccc_project
                 ON {code_content_chunks}(project_id);"
            ),
        ),
        object(
            "idx_ccc_file index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_ccc_file
                 ON {code_content_chunks}(project_id, file_path);"
            ),
        ),
        object(
            "code_imports table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_imports} (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    project_id UUID NOT NULL,
                    source_file TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    target_module TEXT NOT NULL,
                    CONSTRAINT code_imports_project_source_file_hash_target_module_key
                    UNIQUE (project_id, source_file, content_hash, target_module),
                    FOREIGN KEY (project_id, source_file, content_hash)
                        REFERENCES {code_indexed_files}(project_id, file_path, content_hash)
                        ON DELETE CASCADE
                );"
            ),
        ),
        object(
            "idx_ci_file index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_ci_file
                 ON {code_imports}(project_id, source_file);"
            ),
        ),
        object(
            "code_calls table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_calls} (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    project_id UUID NOT NULL,
                    caller_symbol_id UUID,
                    callee_symbol_id UUID,
                    callee_name TEXT NOT NULL,
                    callee_target_kind TEXT NOT NULL DEFAULT 'unresolved',
                    callee_external_module TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    line INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT code_calls_unique_call_target
                    UNIQUE NULLS NOT DISTINCT (
                        project_id, file_path, content_hash, caller_symbol_id,
                        callee_symbol_id, callee_name, callee_target_kind,
                        callee_external_module, line
                    ),
                    FOREIGN KEY (project_id, file_path, content_hash)
                        REFERENCES {code_indexed_files}(project_id, file_path, content_hash)
                        ON DELETE CASCADE
                );"
            ),
        ),
        object(
            "idx_cc_file index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cc_file
                 ON {code_calls}(project_id, file_path);"
            ),
        ),
        object(
            "idx_cc_caller index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cc_caller
                 ON {code_calls}(project_id, caller_symbol_id);"
            ),
        ),
        object(
            "idx_cc_target index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cc_target
                 ON {code_calls}(project_id, callee_target_kind, callee_symbol_id, callee_name);"
            ),
        ),
        object(
            "code_inheritance table",
            ExternalPostgresObjectKind::Table,
            format!(
                "CREATE TABLE IF NOT EXISTS {code_inheritance} (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    project_id UUID NOT NULL,
                    source_symbol_id UUID,
                    source_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL DEFAULT 'symbol',
                    source_external_module TEXT NOT NULL DEFAULT '',
                    target_symbol_id UUID,
                    target_name TEXT NOT NULL,
                    target_kind TEXT NOT NULL DEFAULT 'unresolved',
                    target_external_module TEXT NOT NULL DEFAULT '',
                    heritage_kind TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    line INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT code_inheritance_unique_target
                    UNIQUE NULLS NOT DISTINCT (
                        project_id, file_path, content_hash,
                        source_symbol_id, source_name, source_kind, source_external_module,
                        target_symbol_id, target_name, target_kind, target_external_module,
                        heritage_kind, line
                    ),
                    CONSTRAINT code_inheritance_heritage_kind_check
                    CHECK (heritage_kind IN ('INHERITS', 'EXTENDS', 'IMPLEMENTS')),
                    FOREIGN KEY (project_id, file_path, content_hash)
                        REFERENCES {code_indexed_files}(project_id, file_path, content_hash)
                        ON DELETE CASCADE
                );"
            ),
        ),
        object(
            "idx_cinherit_file index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cinherit_file
                 ON {code_inheritance}(project_id, file_path);"
            ),
        ),
        object(
            "idx_cinherit_source index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cinherit_source
                 ON {code_inheritance}(project_id, source_symbol_id);"
            ),
        ),
        object(
            "idx_cinherit_target index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS idx_cinherit_target
                 ON {code_inheritance}(project_id, target_kind, target_symbol_id, target_name);"
            ),
        ),
        object(
            "code_symbols_search_bm25 index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS code_symbols_search_bm25
                 ON {code_symbols}
                 USING bm25 (id, name, qualified_name, signature, docstring, summary)
                 WITH (key_field = 'id');"
            ),
        ),
        object(
            "code_content_search_bm25 index",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS code_content_search_bm25
                 ON {code_content_chunks}
                 USING bm25 (id, content)
                 WITH (key_field = 'id');"
            ),
        ),
    ])
}

pub fn gwiki_postgres_objects(
    schema: &str,
) -> Result<Vec<ExternalPostgresObject>, ExternalSchemaError> {
    let documents = qualified_relation(schema, "gwiki_documents")?;
    let chunks = qualified_relation(schema, "gwiki_chunks")?;
    let links = qualified_relation(schema, "gwiki_links")?;
    let sources = qualified_relation(schema, "gwiki_sources")?;
    let ingestions = qualified_relation(schema, "gwiki_ingestions")?;

    Ok(vec![
        object(
            "gwiki_pg_search_extension_preflight",
            ExternalPostgresObjectKind::Preflight,
            "DO $$
             BEGIN
                 IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_search') THEN
                     RAISE EXCEPTION 'ParadeDB pg_search extension is required for gwiki BM25 indexes';
                 END IF;
             END
             $$;"
            .to_string(),
        ),
        object(
            "gwiki_documents",
            ExternalPostgresObjectKind::Table,
            format!(
                r#"CREATE TABLE IF NOT EXISTS {documents} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id UUID,
    topic_name TEXT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    body TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
        ),
        object(
            "gwiki_chunks",
            ExternalPostgresObjectKind::Table,
            format!(
                r#"CREATE TABLE IF NOT EXISTS {chunks} (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id UUID,
    topic_name TEXT,
    path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    heading_path TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
        ),
        object(
            "gwiki_links",
            ExternalPostgresObjectKind::Table,
            format!(
                r#"CREATE TABLE IF NOT EXISTS {links} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id UUID,
    topic_name TEXT,
    path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    link_text TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
        ),
        object(
            "gwiki_sources",
            ExternalPostgresObjectKind::Table,
            format!(
                r#"CREATE TABLE IF NOT EXISTS {sources} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id UUID,
    topic_name TEXT,
    path TEXT NOT NULL,
    document_path TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
        ),
        object(
            "gwiki_ingestions",
            ExternalPostgresObjectKind::Table,
            format!(
                r#"CREATE TABLE IF NOT EXISTS {ingestions} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id UUID,
    topic_name TEXT,
    path TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    -- Nullable: Deleted/Skipped ingestion events have no current content to hash.
    content_hash TEXT,
    frontmatter JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    status TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
        ),
        object(
            "gwiki_documents_scope_path_idx",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {documents} (scope_kind, scope_id, path);",
                quote_identifier("gwiki_documents_scope_path_idx", "index")?
            ),
        ),
        object(
            "gwiki_documents_content_hash_idx",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS {} ON {documents} (scope_kind, scope_id, content_hash);",
                quote_identifier("gwiki_documents_content_hash_idx", "index")?
            ),
        ),
        object(
            "gwiki_chunks_scope_path_idx",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS {} ON {chunks} (scope_kind, scope_id, path, chunk_index);",
                quote_identifier("gwiki_chunks_scope_path_idx", "index")?
            ),
        ),
        object(
            "gwiki_sources_scope_path_idx",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {sources} (scope_kind, scope_id, document_path);",
                quote_identifier("gwiki_sources_scope_path_idx", "index")?
            ),
        ),
        object(
            "gwiki_links_scope_idx",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {links} (scope_kind, scope_id, path, target_path, link_text, link_kind);",
                quote_identifier("gwiki_links_scope_idx", "index")?
            ),
        ),
        object(
            "gwiki_documents_search_bm25",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS {} ON {documents} USING bm25 (id, path, title, body) WITH (key_field = 'id');",
                quote_identifier("gwiki_documents_search_bm25", "index")?
            ),
        ),
        object(
            "gwiki_chunks_search_bm25",
            ExternalPostgresObjectKind::Index,
            format!(
                "CREATE INDEX IF NOT EXISTS {} ON {chunks} USING bm25 (id, path, content) WITH (key_field = 'id');",
                quote_identifier("gwiki_chunks_search_bm25", "index")?
            ),
        ),
    ])
}

fn object(
    name: &'static str,
    kind: ExternalPostgresObjectKind,
    sql: String,
) -> ExternalPostgresObject {
    ExternalPostgresObject { name, kind, sql }
}

fn qualified_relation(schema: &str, relation: &str) -> Result<String, ExternalSchemaError> {
    Ok(format!(
        "{}.{}",
        quote_identifier(schema, "schema")?,
        quote_identifier(relation, "relation")?
    ))
}

fn quote_identifier(value: &str, label: &str) -> Result<String, ExternalSchemaError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(identifier_error(
            label,
            format!("{label} identifier must not be empty"),
        ));
    }
    if trimmed.contains('\0') {
        return Err(identifier_error(
            label,
            format!("{label} identifier must not contain NUL bytes"),
        ));
    }
    if trimmed.len() > POSTGRES_IDENTIFIER_MAX_BYTES {
        return Err(identifier_error(
            label,
            format!("{label} identifier must be at most {POSTGRES_IDENTIFIER_MAX_BYTES} bytes"),
        ));
    }
    let escaped = trimmed.replace('"', "\"\"");
    Ok(format!("\"{escaped}\""))
}

fn identifier_error(label: &str, message: String) -> ExternalSchemaError {
    ExternalSchemaError {
        object: label.to_string(),
        message,
    }
}
