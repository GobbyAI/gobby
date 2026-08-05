use gobby_core::schema::{ExternalPostgresObjectKind, gwiki_postgres_objects};
use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};

pub const NAMESPACE: &str = "gwiki";
pub const DEFAULT_SCHEMA: &str = "public";
pub const SETUP_OWNERSHIP_NOTE: &str = "gwiki setup is owned by `crates/gwiki/src/setup.rs`";

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

    #[allow(dead_code, reason = "reserved gwiki CLI/API split")]
    pub fn schema(&self) -> &str {
        &self.schema
    }

    pub fn postgres_objects(&self) -> Result<Vec<GwikiPostgresObject>, SetupError> {
        gwiki_postgres_objects(&self.schema)
            .map(|objects| {
                objects
                    .into_iter()
                    .map(|object| GwikiPostgresObject {
                        name: object.name,
                        kind: match object.kind {
                            ExternalPostgresObjectKind::Preflight => {
                                GwikiPostgresObjectKind::Preflight
                            }
                            ExternalPostgresObjectKind::Table => GwikiPostgresObjectKind::Table,
                            ExternalPostgresObjectKind::Index => GwikiPostgresObjectKind::Index,
                        },
                        sql: object.sql,
                    })
                    .collect()
            })
            .map_err(|error| SetupError::CreationFailed {
                object: error.object,
                message: error.message,
            })
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
    fn standalone_setup_ddl_is_exported_by_gcore() {
        use gobby_core::schema::gwiki_postgres_objects;

        let local = GwikiStandaloneSetup::new()
            .postgres_objects()
            .expect("local postgres objects");
        let exported =
            gwiki_postgres_objects(DEFAULT_SCHEMA).expect("gcore postgres object definitions");

        let local = local
            .iter()
            .map(|object| (object.name, object.sql.as_str()))
            .collect::<Vec<_>>();
        let exported = exported
            .iter()
            .map(|object| (object.name, object.sql.as_str()))
            .collect::<Vec<_>>();

        assert_eq!(local, exported);
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
    fn external_schema_builder_rejects_names_over_postgres_byte_limit() {
        let name = "a".repeat(64);
        let error = gwiki_postgres_objects(&name).expect_err("identifier is too long");

        assert!(error.to_string().contains("at most 63 bytes"));
    }

    #[test]
    fn external_schema_builder_accepts_quoted_name_at_raw_byte_limit() {
        let name = format!("{}\"", "a".repeat(62));
        gwiki_postgres_objects(&name).expect("raw identifier is within PostgreSQL's byte limit");
    }
}
