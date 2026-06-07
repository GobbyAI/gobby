use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};

pub const NAMESPACE: &str = "gwiki";
pub const DEFAULT_SCHEMA: &str = "public";
pub const SETUP_OWNERSHIP_NOTE: &str = "gwiki setup is owned by `crates/gwiki/src/setup.rs`";
const POSTGRES_IDENTIFIER_MAX_BYTES: usize = 63;

pub const GWIKI_POSTGRES_TABLES: &[GwikiTable] = &[
    GwikiTable::Documents,
    GwikiTable::Chunks,
    GwikiTable::Links,
    GwikiTable::Sources,
    GwikiTable::Ingestions,
];

pub const GWIKI_POSTGRES_INDEXES: &[&str] = &[
    "gwiki_documents_scope_path_idx",
    "gwiki_documents_content_hash_idx",
    "gwiki_chunks_scope_path_idx",
    "gwiki_sources_scope_path_idx",
    "gwiki_links_scope_idx",
    "gwiki_documents_search_bm25",
    "gwiki_chunks_search_bm25",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GwikiTable {
    Documents,
    Chunks,
    Links,
    Sources,
    Ingestions,
}

impl GwikiTable {
    pub fn name(self) -> &'static str {
        match self {
            Self::Documents => "gwiki_documents",
            Self::Chunks => "gwiki_chunks",
            Self::Links => "gwiki_links",
            Self::Sources => "gwiki_sources",
            Self::Ingestions => "gwiki_ingestions",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GwikiPostgresObjectKind {
    Preflight,
    Table,
    Index,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GwikiPostgresObject {
    pub name: &'static str,
    pub kind: GwikiPostgresObjectKind,
    pub sql: String,
}

#[derive(Debug, Clone)]
pub struct GwikiStandaloneSetup {
    schema: String,
}

impl GwikiStandaloneSetup {
    pub fn new() -> Self {
        Self {
            schema: DEFAULT_SCHEMA.to_string(),
        }
    }

    pub fn schema(&self) -> &str {
        &self.schema
    }

    pub fn postgres_objects(&self) -> Result<Vec<GwikiPostgresObject>, SetupError> {
        let mut objects = vec![preflight(
            "gwiki_pg_search_extension_preflight",
            "DO $$
             BEGIN
                 IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_search') THEN
                     RAISE EXCEPTION 'ParadeDB pg_search extension is required for gwiki BM25 indexes';
                 END IF;
             END
             $$;"
            .to_string(),
        )];

        for &gwiki_table in GWIKI_POSTGRES_TABLES {
            objects.push(table(gwiki_table.name(), self.table_sql(gwiki_table)?));
        }
        for &index_name in GWIKI_POSTGRES_INDEXES {
            objects.push(index(index_name, self.index_sql(index_name)?));
        }

        Ok(objects)
    }

    fn table_sql(&self, table: GwikiTable) -> Result<String, SetupError> {
        let relation = self.qualified_relation_name(table.name(), "table")?;
        Ok(match table {
            GwikiTable::Documents => format!(
                r#"CREATE TABLE IF NOT EXISTS {relation} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id TEXT,
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
            GwikiTable::Chunks => format!(
                r#"CREATE TABLE IF NOT EXISTS {relation} (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id TEXT,
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
            GwikiTable::Links => format!(
                r#"CREATE TABLE IF NOT EXISTS {relation} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id TEXT,
    topic_name TEXT,
    path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    link_text TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
            GwikiTable::Sources => format!(
                r#"CREATE TABLE IF NOT EXISTS {relation} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id TEXT,
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
            GwikiTable::Ingestions => format!(
                r#"CREATE TABLE IF NOT EXISTS {relation} (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    project_id TEXT,
    topic_name TEXT,
    path TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    frontmatter JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    status TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"#
            ),
        })
    }

    fn index_sql(&self, index_name: &str) -> Result<String, SetupError> {
        let index = quote_identifier(index_name, "index")?;
        let documents = self.qualified_relation_name(GwikiTable::Documents.name(), "table")?;
        let chunks = self.qualified_relation_name(GwikiTable::Chunks.name(), "table")?;
        let links = self.qualified_relation_name(GwikiTable::Links.name(), "table")?;
        let sources = self.qualified_relation_name(GwikiTable::Sources.name(), "table")?;

        match index_name {
            "gwiki_documents_scope_path_idx" => Ok(format!(
                "CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {documents} (scope_kind, scope_id, path);"
            )),
            "gwiki_documents_content_hash_idx" => Ok(format!(
                "CREATE INDEX IF NOT EXISTS {index} ON {documents} (scope_kind, scope_id, content_hash);"
            )),
            "gwiki_chunks_scope_path_idx" => Ok(format!(
                "CREATE INDEX IF NOT EXISTS {index} ON {chunks} (scope_kind, scope_id, path, chunk_index);"
            )),
            "gwiki_sources_scope_path_idx" => Ok(format!(
                "CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {sources} (scope_kind, scope_id, document_path);"
            )),
            "gwiki_links_scope_idx" => Ok(format!(
                "CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {links} (scope_kind, scope_id, path, target_path, link_text, link_kind);"
            )),
            "gwiki_documents_search_bm25" => Ok(format!(
                "CREATE INDEX IF NOT EXISTS {index} ON {documents} USING bm25 (id, path, title, body) WITH (key_field = 'id');"
            )),
            "gwiki_chunks_search_bm25" => Ok(format!(
                "CREATE INDEX IF NOT EXISTS {index} ON {chunks} USING bm25 (id, path, content) WITH (key_field = 'id');"
            )),
            other => Err(SetupError::CreationFailed {
                object: other.to_string(),
                message: "unknown gwiki PostgreSQL index".to_string(),
            }),
        }
    }

    fn qualified_relation_name(&self, relation: &str, label: &str) -> Result<String, SetupError> {
        Ok(format!(
            "{}.{}",
            quote_identifier(&self.schema, "schema")?,
            quote_identifier(relation, label)?
        ))
    }
}

impl StandaloneSetup for GwikiStandaloneSetup {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    fn owned_objects(&self) -> Result<Vec<OwnedObject>, SetupError> {
        Ok(self
            .postgres_objects()?
            .into_iter()
            .map(owned_object)
            .collect())
    }

    fn create(&self, ctx: &mut SetupContext<'_>) -> Result<SetupReport, SetupError> {
        let mut report = SetupReport::default();
        // Objects are created with `IF NOT EXISTS`, so setup is idempotent without
        // holding a single explicit transaction across all gwiki-owned DDL.
        for mut object in self.owned_objects()? {
            match (object.creator)(ctx) {
                Ok(()) => report.created.push(object.name),
                Err(err) => {
                    report.failed.push((object.name, err.to_string()));
                    break;
                }
            }
        }
        Ok(report)
    }
}

pub fn default_setup() -> GwikiStandaloneSetup {
    GwikiStandaloneSetup::new()
}

fn table(name: &'static str, sql: String) -> GwikiPostgresObject {
    GwikiPostgresObject {
        name,
        kind: GwikiPostgresObjectKind::Table,
        sql,
    }
}

fn index(name: &'static str, sql: String) -> GwikiPostgresObject {
    GwikiPostgresObject {
        name,
        kind: GwikiPostgresObjectKind::Index,
        sql,
    }
}

fn preflight(name: &'static str, sql: String) -> GwikiPostgresObject {
    GwikiPostgresObject {
        name,
        kind: GwikiPostgresObjectKind::Preflight,
        sql,
    }
}

fn owned_object(object: GwikiPostgresObject) -> OwnedObject {
    let object_name = object.name.to_string();
    let sql = object.sql;
    OwnedObject {
        name: object_name.clone(),
        store: StoreKind::Postgres,
        creator: Box::new(move |ctx| execute_postgres_ddl(ctx, &object_name, &sql)),
    }
}

fn execute_postgres_ddl(
    ctx: &mut SetupContext<'_>,
    object: &str,
    sql: &str,
) -> Result<(), SetupError> {
    let Some(pg) = ctx.pg.as_deref_mut() else {
        return Err(SetupError::ConnectionFailed {
            store: "postgres".to_string(),
            message: "PostgreSQL connection was not supplied to setup context".to_string(),
        });
    };

    pg.batch_execute(sql).map_err(|err| {
        let message = err
            .as_db_error()
            .map(|db_error| db_error.message().to_string())
            .unwrap_or_else(|| err.to_string());
        SetupError::CreationFailed {
            object: object.to_string(),
            message,
        }
    })
}

fn quote_identifier(value: &str, label: &str) -> Result<String, SetupError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!("{label} identifier must not be empty"),
        });
    }
    if trimmed.contains('\0') {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!("{label} identifier must not contain NUL bytes"),
        });
    }
    let escaped = trimmed.replace('"', "\"\"");
    if escaped.len() > POSTGRES_IDENTIFIER_MAX_BYTES {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!(
                "{label} identifier must be at most {POSTGRES_IDENTIFIER_MAX_BYTES} bytes"
            ),
        });
    }
    Ok(format!("\"{escaped}\""))
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::setup::StandaloneSetup;

    #[test]
    fn setup_creates_only_gwiki_owned_objects() {
        assert_eq!(
            SETUP_OWNERSHIP_NOTE,
            "gwiki setup is owned by `crates/gwiki/src/setup.rs`"
        );

        let setup = GwikiStandaloneSetup::new();
        assert_eq!(setup.namespace(), "gwiki");
        assert_eq!(setup.schema(), "public");

        let objects = setup.postgres_objects().expect("setup objects");
        assert!(!objects.is_empty());
        assert!(
            objects
                .iter()
                .all(|object| object.name.starts_with("gwiki_")),
            "all setup relations must be gwiki-owned: {objects:?}"
        );
        assert!(
            objects
                .iter()
                .any(|object| object.kind == GwikiPostgresObjectKind::Table)
        );
        assert!(
            objects
                .iter()
                .any(|object| object.kind == GwikiPostgresObjectKind::Index)
        );

        let combined_sql = objects
            .iter()
            .map(|object| object.sql.as_str())
            .collect::<Vec<_>>()
            .join("\n");

        for forbidden in [
            "config_store",
            "schema_migrations",
            "code_symbols",
            "code_content_chunks",
            ".gobby/project.json",
        ] {
            assert!(!combined_sql.contains(forbidden), "{combined_sql}");
        }

        assert!(combined_sql.contains("pg_extension"), "{combined_sql}");
        for table in GWIKI_POSTGRES_TABLES {
            assert!(combined_sql.contains(table.name()), "{combined_sql}");
        }
        for relation in GWIKI_POSTGRES_INDEXES {
            assert!(combined_sql.contains(relation), "{combined_sql}");
        }
        assert!(!combined_sql.contains("CREATE EXTENSION"), "{combined_sql}");
        assert!(!combined_sql.contains("CREATE SCHEMA"), "{combined_sql}");
        assert!(
            combined_sql.contains("CREATE TABLE IF NOT EXISTS"),
            "{combined_sql}"
        );
        assert!(
            combined_sql.contains("CREATE UNIQUE INDEX IF NOT EXISTS"),
            "{combined_sql}"
        );
        assert!(combined_sql.contains("USING bm25"), "{combined_sql}");
        assert!(
            combined_sql.contains("WITH (key_field = 'id')"),
            "{combined_sql}"
        );
        assert!(
            combined_sql
                .contains("(scope_kind, scope_id, path, target_path, link_text, link_kind)"),
            "{combined_sql}"
        );
        assert!(!combined_sql.contains("ALTER "), "{combined_sql}");
        assert!(!combined_sql.contains("DROP "), "{combined_sql}");

        let owned_names = setup
            .owned_objects()
            .expect("owned objects")
            .into_iter()
            .map(|object| {
                assert_eq!(object.store, StoreKind::Postgres);
                object.name
            })
            .collect::<Vec<_>>();
        assert_eq!(
            owned_names,
            objects
                .iter()
                .map(|object| object.name.to_string())
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn published_lists_match_generated_objects() {
        let objects = GwikiStandaloneSetup::new()
            .postgres_objects()
            .expect("setup objects");
        let tables = objects
            .iter()
            .filter(|object| object.kind == GwikiPostgresObjectKind::Table)
            .map(|object| object.name)
            .collect::<Vec<_>>();
        let indexes = objects
            .iter()
            .filter(|object| object.kind == GwikiPostgresObjectKind::Index)
            .map(|object| object.name)
            .collect::<Vec<_>>();

        assert_eq!(
            tables,
            GWIKI_POSTGRES_TABLES
                .iter()
                .map(|table| table.name())
                .collect::<Vec<_>>()
        );
        assert_eq!(indexes, GWIKI_POSTGRES_INDEXES);
    }

    #[test]
    fn quote_identifier_rejects_names_over_postgres_byte_limit() {
        let name = "a".repeat(64);
        let error = quote_identifier(&name, "schema").expect_err("identifier is too long");

        assert!(error.to_string().contains("at most 63 bytes"));
    }

    #[test]
    fn quote_identifier_rejects_escaped_names_over_postgres_byte_limit() {
        let name = format!("{}\"", "a".repeat(62));
        let error = quote_identifier(&name, "schema").expect_err("escaped identifier is too long");

        assert!(error.to_string().contains("at most 63 bytes"));
    }
}
