use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};

pub const NAMESPACE: &str = "gwiki";
pub const DEFAULT_SCHEMA: &str = "public";
pub const SETUP_OWNERSHIP_NOTE: &str = "gwiki setup is owned by `crates/gwiki/src/setup.rs`";
const POSTGRES_IDENTIFIER_MAX_BYTES: usize = 63;

pub const GWIKI_POSTGRES_TABLES: &[&str] = &[
    "gwiki_documents",
    "gwiki_chunks",
    "gwiki_links",
    "gwiki_sources",
    "gwiki_ingestions",
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
pub enum GwikiPostgresObjectKind {
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
    pub fn new(schema: impl Into<String>) -> Self {
        Self {
            schema: schema.into(),
        }
    }

    pub fn schema(&self) -> &str {
        &self.schema
    }

    pub fn postgres_objects(&self) -> Result<Vec<GwikiPostgresObject>, SetupError> {
        let documents = self.qualified("gwiki_documents", "table")?;
        let chunks = self.qualified("gwiki_chunks", "table")?;
        let links = self.qualified("gwiki_links", "table")?;
        let sources = self.qualified("gwiki_sources", "table")?;
        let ingestions = self.qualified("gwiki_ingestions", "table")?;

        let documents_scope_path_idx = self.qualified("gwiki_documents_scope_path_idx", "index")?;
        let documents_content_hash_idx =
            self.qualified("gwiki_documents_content_hash_idx", "index")?;
        let chunks_scope_path_idx = self.qualified("gwiki_chunks_scope_path_idx", "index")?;
        let sources_scope_path_idx = self.qualified("gwiki_sources_scope_path_idx", "index")?;
        let links_scope_idx = self.qualified("gwiki_links_scope_idx", "index")?;
        let documents_search_bm25 = self.qualified("gwiki_documents_search_bm25", "index")?;
        let chunks_search_bm25 = self.qualified("gwiki_chunks_search_bm25", "index")?;

        Ok(vec![
            table(
                "gwiki_documents",
                format!(
                    "CREATE TABLE IF NOT EXISTS {documents} (
                        id TEXT PRIMARY KEY,
                        scope_kind TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        project_id TEXT,
                        topic_name TEXT,
                        path TEXT NOT NULL,
                        title TEXT,
                        source_kind TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        frontmatter JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        body TEXT NOT NULL DEFAULT '',
                        indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (scope_kind, scope_id, path)
                    );"
                ),
            ),
            table(
                "gwiki_chunks",
                format!(
                    "CREATE TABLE IF NOT EXISTS {chunks} (
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
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (scope_kind, scope_id, path, chunk_index)
                    );"
                ),
            ),
            table(
                "gwiki_links",
                format!(
                    "CREATE TABLE IF NOT EXISTS {links} (
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
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (scope_kind, scope_id, path, target_path, link_text, link_kind)
                    );"
                ),
            ),
            table(
                "gwiki_sources",
                format!(
                    "CREATE TABLE IF NOT EXISTS {sources} (
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
                        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (scope_kind, scope_id, path)
                    );"
                ),
            ),
            table(
                "gwiki_ingestions",
                format!(
                    "CREATE TABLE IF NOT EXISTS {ingestions} (
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
                    );"
                ),
            ),
            index(
                "gwiki_documents_scope_path_idx",
                format!(
                    "CREATE INDEX IF NOT EXISTS {documents_scope_path_idx}
                     ON {documents}(scope_kind, scope_id, path);"
                ),
            ),
            index(
                "gwiki_documents_content_hash_idx",
                format!(
                    "CREATE INDEX IF NOT EXISTS {documents_content_hash_idx}
                     ON {documents}(scope_kind, scope_id, content_hash);"
                ),
            ),
            index(
                "gwiki_chunks_scope_path_idx",
                format!(
                    "CREATE INDEX IF NOT EXISTS {chunks_scope_path_idx}
                     ON {chunks}(scope_kind, scope_id, path);"
                ),
            ),
            index(
                "gwiki_sources_scope_path_idx",
                format!(
                    "CREATE INDEX IF NOT EXISTS {sources_scope_path_idx}
                     ON {sources}(scope_kind, scope_id, path);"
                ),
            ),
            index(
                "gwiki_links_scope_idx",
                format!(
                    "CREATE INDEX IF NOT EXISTS {links_scope_idx}
                     ON {links}(scope_kind, scope_id, target_path);"
                ),
            ),
            index(
                "gwiki_documents_search_bm25",
                format!(
                    "CREATE INDEX IF NOT EXISTS {documents_search_bm25}
                     ON {documents}
                     USING bm25 (id, path, title, body)
                     WITH (key_field = 'id');"
                ),
            ),
            index(
                "gwiki_chunks_search_bm25",
                format!(
                    "CREATE INDEX IF NOT EXISTS {chunks_search_bm25}
                     ON {chunks}
                     USING bm25 (id, path, content)
                     WITH (key_field = 'id');"
                ),
            ),
        ])
    }

    fn qualified(&self, relation: &str, label: &str) -> Result<String, SetupError> {
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
    GwikiStandaloneSetup::new(DEFAULT_SCHEMA)
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

    pg.batch_execute(sql)
        .map_err(|err| SetupError::CreationFailed {
            object: object.to_string(),
            message: err.to_string(),
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
    if trimmed.len() > POSTGRES_IDENTIFIER_MAX_BYTES {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!(
                "{label} identifier must be at most {POSTGRES_IDENTIFIER_MAX_BYTES} bytes"
            ),
        });
    }
    Ok(format!("\"{}\"", trimmed.replace('"', "\"\"")))
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

        let setup = GwikiStandaloneSetup::new("public");
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

        assert!(combined_sql.contains("scope_kind"), "{combined_sql}");
        assert!(combined_sql.contains("project_id"), "{combined_sql}");
        assert!(combined_sql.contains("topic_name"), "{combined_sql}");
        assert!(combined_sql.contains("path"), "{combined_sql}");
        assert!(combined_sql.contains("source_kind"), "{combined_sql}");
        assert!(combined_sql.contains("content_hash"), "{combined_sql}");
        assert!(combined_sql.contains("frontmatter"), "{combined_sql}");
        assert!(combined_sql.contains("provenance"), "{combined_sql}");
        assert!(combined_sql.contains("USING bm25"), "{combined_sql}");
        assert!(!combined_sql.contains("CREATE EXTENSION"), "{combined_sql}");
        assert!(!combined_sql.contains("CREATE SCHEMA"), "{combined_sql}");
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
        let objects = GwikiStandaloneSetup::new("public")
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

        assert_eq!(tables, GWIKI_POSTGRES_TABLES);
        assert_eq!(indexes, GWIKI_POSTGRES_INDEXES);
    }

    #[test]
    fn quote_identifier_rejects_names_over_postgres_byte_limit() {
        let name = "a".repeat(64);
        let error = quote_identifier(&name, "schema").expect_err("identifier is too long");

        assert!(error.to_string().contains("at most 63 bytes"));
    }
}
