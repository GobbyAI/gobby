use super::contracts::NAMESPACE;
use super::identifiers::qualified_relation;
use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GcodeStandaloneSetup {
    schema: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PostgresObjectDefinition {
    pub(crate) name: String,
    pub(crate) sql: String,
}

impl GcodeStandaloneSetup {
    pub fn new(schema: impl Into<String>) -> Self {
        Self {
            schema: schema.into(),
        }
    }

    pub fn schema(&self) -> &str {
        &self.schema
    }

    fn object_definition(&self, name: &str, sql: String) -> PostgresObjectDefinition {
        PostgresObjectDefinition {
            name: name.to_string(),
            sql,
        }
    }

    fn qualified(&self, relation: &str) -> Result<String, SetupError> {
        qualified_relation(&self.schema, relation, "relation")
    }

    pub(crate) fn postgres_object_definitions(
        &self,
    ) -> Result<Vec<PostgresObjectDefinition>, SetupError> {
        let code_indexed_projects = self.qualified("code_indexed_projects")?;
        let code_indexed_files = self.qualified("code_indexed_files")?;
        let code_symbols = self.qualified("code_symbols")?;
        let code_content_chunks = self.qualified("code_content_chunks")?;
        let code_imports = self.qualified("code_imports")?;
        let code_calls = self.qualified("code_calls")?;

        Ok(vec![
            self.object_definition(
                "pg_search extension",
                "CREATE EXTENSION IF NOT EXISTS pg_search;".to_string(),
            ),
            self.object_definition(
                "code_indexed_projects table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_indexed_projects} (
                        id TEXT PRIMARY KEY,
                        root_path TEXT NOT NULL,
                        total_files INTEGER NOT NULL DEFAULT 0,
                        total_symbols INTEGER NOT NULL DEFAULT 0,
                        last_indexed_at TIMESTAMPTZ,
                        index_duration_ms INTEGER,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );"
                ),
            ),
            self.object_definition(
                "code_indexed_files table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_indexed_files} (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
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
                        UNIQUE (project_id, file_path)
                    );"
                ),
            ),
            self.object_definition(
                "idx_cif_project index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cif_project
                     ON {code_indexed_files}(project_id);"
                ),
            ),
            self.object_definition(
                "idx_cif_graph_synced index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cif_graph_synced
                     ON {code_indexed_files}(project_id, graph_synced);"
                ),
            ),
            self.object_definition(
                "idx_cif_vectors_synced index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cif_vectors_synced
                     ON {code_indexed_files}(project_id, vectors_synced);"
                ),
            ),
            self.object_definition(
                "code_symbols table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_symbols} (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
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
                        parent_symbol_id TEXT,
                        content_hash TEXT NOT NULL,
                        summary TEXT,
                        summary_attempted_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );"
                ),
            ),
            self.object_definition(
                "idx_cs_project index",
                format!("CREATE INDEX IF NOT EXISTS idx_cs_project ON {code_symbols}(project_id);"),
            ),
            self.object_definition(
                "idx_cs_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cs_file
                     ON {code_symbols}(project_id, file_path);"
                ),
            ),
            self.object_definition(
                "idx_cs_name index",
                format!("CREATE INDEX IF NOT EXISTS idx_cs_name ON {code_symbols}(name);"),
            ),
            self.object_definition(
                "idx_cs_qualified index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cs_qualified
                     ON {code_symbols}(qualified_name);"
                ),
            ),
            self.object_definition(
                "idx_cs_kind index",
                format!("CREATE INDEX IF NOT EXISTS idx_cs_kind ON {code_symbols}(kind);"),
            ),
            self.object_definition(
                "idx_cs_parent index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cs_parent
                     ON {code_symbols}(parent_symbol_id);"
                ),
            ),
            self.object_definition(
                "code_content_chunks table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_content_chunks} (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        line_start INTEGER NOT NULL,
                        line_end INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        language TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (project_id, file_path, chunk_index)
                    );"
                ),
            ),
            self.object_definition(
                "idx_ccc_project index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_ccc_project
                     ON {code_content_chunks}(project_id);"
                ),
            ),
            self.object_definition(
                "idx_ccc_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_ccc_file
                     ON {code_content_chunks}(project_id, file_path);"
                ),
            ),
            self.object_definition(
                "code_imports table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_imports} (
                        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        source_file TEXT NOT NULL,
                        target_module TEXT NOT NULL,
                        UNIQUE (project_id, source_file, target_module)
                    );"
                ),
            ),
            self.object_definition(
                "idx_ci_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_ci_file
                     ON {code_imports}(project_id, source_file);"
                ),
            ),
            self.object_definition(
                "code_calls table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_calls} (
                        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        caller_symbol_id TEXT NOT NULL,
                        callee_symbol_id TEXT NOT NULL DEFAULT '',
                        callee_name TEXT NOT NULL,
                        callee_target_kind TEXT NOT NULL DEFAULT 'unresolved',
                        callee_external_module TEXT NOT NULL DEFAULT '',
                        file_path TEXT NOT NULL,
                        line INTEGER NOT NULL DEFAULT 0,
                        UNIQUE (
                            project_id, caller_symbol_id, callee_symbol_id, callee_name,
                            callee_target_kind, callee_external_module, file_path, line
                        )
                    );"
                ),
            ),
            self.object_definition(
                "idx_cc_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cc_file
                     ON {code_calls}(project_id, file_path);"
                ),
            ),
            self.object_definition(
                "idx_cc_caller index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cc_caller
                     ON {code_calls}(project_id, caller_symbol_id);"
                ),
            ),
            self.object_definition(
                "idx_cc_target index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cc_target
                     ON {code_calls}(project_id, callee_target_kind, callee_symbol_id, callee_name);"
                ),
            ),
            self.object_definition(
                "code_symbols_search_bm25 index",
                format!(
                    "CREATE INDEX IF NOT EXISTS code_symbols_search_bm25
                     ON {code_symbols}
                     USING bm25 (id, name, qualified_name, signature, docstring, summary)
                     WITH (key_field = 'id');"
                ),
            ),
            self.object_definition(
                "code_content_search_bm25 index",
                format!(
                    "CREATE INDEX IF NOT EXISTS code_content_search_bm25
                     ON {code_content_chunks}
                     USING bm25 (id, content)
                     WITH (key_field = 'id');"
                ),
            ),
        ])
    }
}

impl StandaloneSetup for GcodeStandaloneSetup {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    fn owned_objects(&self) -> Result<Vec<OwnedObject>, SetupError> {
        Ok(self
            .postgres_object_definitions()?
            .into_iter()
            .map(owned_object)
            .collect())
    }

    fn create(&self, ctx: &mut SetupContext<'_>) -> Result<SetupReport, SetupError> {
        let mut report = SetupReport::default();
        let mut objects = self.owned_objects()?.into_iter();
        while let Some(mut object) = objects.next() {
            match (object.creator)(ctx) {
                Ok(()) => report.created.push(object.name),
                Err(err) => {
                    report.failed.push((object.name, err.to_string()));
                    report.skipped.extend(objects.map(|object| object.name));
                    break;
                }
            }
        }
        Ok(report)
    }
}

fn owned_object(definition: PostgresObjectDefinition) -> OwnedObject {
    let object_name = definition.name;
    let sql = definition.sql;
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
