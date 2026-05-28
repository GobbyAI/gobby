use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};
use postgres::Client;
use serde::{Deserialize, Serialize};

const DEFAULT_SCHEMA: &str = "gcode";
const NAMESPACE: &str = "gcode";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub standalone: bool,
    pub database_url: Option<String>,
    pub schema: String,
}

impl StandaloneSetupRequest {
    pub fn new(standalone: bool, database_url: Option<String>, schema: Option<String>) -> Self {
        Self {
            standalone,
            database_url,
            schema: schema.unwrap_or_else(|| DEFAULT_SCHEMA.to_string()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupStatus {
    pub namespace: String,
    pub schema: String,
    pub created: Vec<String>,
    pub skipped: Vec<String>,
    pub failed: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GcodeStandaloneSetup {
    schema: String,
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

    fn object(&self, name: &str, sql: String) -> OwnedObject {
        let object_name = name.to_string();
        OwnedObject {
            name: object_name.clone(),
            store: StoreKind::Postgres,
            creator: Box::new(move |ctx| execute_postgres_ddl(ctx, &object_name, &sql)),
        }
    }

    fn qualified(&self, relation: &str) -> Result<String, SetupError> {
        Ok(format!(
            "{}.{}",
            quote_identifier(&self.schema, "schema")?,
            quote_identifier(relation, "relation")?
        ))
    }
}

impl StandaloneSetup for GcodeStandaloneSetup {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    fn owned_objects(&self) -> Vec<OwnedObject> {
        let schema = match quote_identifier(&self.schema, "schema") {
            Ok(schema) => schema,
            Err(err) => return vec![invalid_object("schema validation", err)],
        };
        let code_indexed_projects = match self.qualified("code_indexed_projects") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_indexed_projects table", err)],
        };
        let code_indexed_files = match self.qualified("code_indexed_files") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_indexed_files table", err)],
        };
        let code_symbols = match self.qualified("code_symbols") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_symbols table", err)],
        };
        let code_content_chunks = match self.qualified("code_content_chunks") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_content_chunks table", err)],
        };
        let code_imports = match self.qualified("code_imports") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_imports table", err)],
        };
        let code_calls = match self.qualified("code_calls") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_calls table", err)],
        };
        let code_graph_sync_state = match self.qualified("code_graph_sync_state") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_graph_sync_state table", err)],
        };
        let code_vector_sync_state = match self.qualified("code_vector_sync_state") {
            Ok(name) => name,
            Err(err) => return vec![invalid_object("code_vector_sync_state table", err)],
        };

        vec![
            self.object(
                "gcode schema",
                format!("CREATE SCHEMA IF NOT EXISTS {schema};"),
            ),
            self.object(
                "code_indexed_projects table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_indexed_projects} (
                        id TEXT PRIMARY KEY,
                        root_path TEXT NOT NULL,
                        total_files INTEGER NOT NULL DEFAULT 0,
                        total_symbols INTEGER NOT NULL DEFAULT 0,
                        last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        index_duration_ms INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );"
                ),
            ),
            self.object(
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
                        indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (project_id, file_path)
                    );"
                ),
            ),
            self.object(
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
                        content_hash TEXT NOT NULL DEFAULT '',
                        summary TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );"
                ),
            ),
            self.object(
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
                        language TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );"
                ),
            ),
            self.object(
                "code_imports table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_imports} (
                        project_id TEXT NOT NULL,
                        source_file TEXT NOT NULL,
                        target_module TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (project_id, source_file, target_module)
                    );"
                ),
            ),
            self.object(
                "code_calls table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_calls} (
                        project_id TEXT NOT NULL,
                        caller_symbol_id TEXT NOT NULL,
                        callee_symbol_id TEXT NOT NULL,
                        callee_name TEXT NOT NULL,
                        callee_target_kind TEXT NOT NULL,
                        callee_external_module TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        line INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (
                            project_id, caller_symbol_id, callee_symbol_id, callee_name,
                            callee_target_kind, callee_external_module, file_path, line
                        )
                    );"
                ),
            ),
            self.object(
                "code_graph_sync_state table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_graph_sync_state} (
                        project_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        synced BOOLEAN NOT NULL DEFAULT FALSE,
                        attempted_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (project_id, file_path)
                    );"
                ),
            ),
            self.object(
                "code_vector_sync_state table",
                format!(
                    "CREATE TABLE IF NOT EXISTS {code_vector_sync_state} (
                        project_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        synced BOOLEAN NOT NULL DEFAULT FALSE,
                        attempted_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (project_id, file_path)
                    );"
                ),
            ),
            self.object(
                "code_symbols_search_bm25 index",
                format!(
                    "CREATE INDEX IF NOT EXISTS code_symbols_search_bm25
                     ON {code_symbols}
                     USING bm25 (id, name, qualified_name, kind, language, signature, docstring, summary)
                     WITH (key_field = 'id');"
                ),
            ),
            self.object(
                "code_content_search_bm25 index",
                format!(
                    "CREATE INDEX IF NOT EXISTS code_content_search_bm25
                     ON {code_content_chunks}
                     USING bm25 (id, content, language)
                     WITH (key_field = 'id');"
                ),
            ),
        ]
    }

    fn create(&self, ctx: &mut SetupContext<'_>) -> Result<SetupReport, SetupError> {
        let mut report = SetupReport::default();
        for mut object in self.owned_objects() {
            match (object.creator)(ctx) {
                Ok(()) => report.created.push(object.name),
                Err(err) => {
                    report.failed.push((object.name, err.to_string()));
                    return Err(err);
                }
            }
        }
        Ok(report)
    }
}

pub fn run_standalone_setup(
    request: &StandaloneSetupRequest,
    client: &mut Client,
) -> Result<StandaloneSetupStatus, SetupError> {
    if !request.standalone {
        return Err(SetupError::AttachedModeRefused);
    }

    let setup = GcodeStandaloneSetup::new(request.schema.clone());
    let mut ctx = SetupContext {
        pg: Some(client),
        falkor_config: None,
        qdrant_config: None,
        non_interactive: true,
    };
    let report = setup.create(&mut ctx)?;

    Ok(StandaloneSetupStatus {
        namespace: setup.namespace().to_string(),
        schema: setup.schema().to_string(),
        created: report.created,
        skipped: report.skipped,
        failed: report.failed,
    })
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

fn invalid_object(name: &str, err: SetupError) -> OwnedObject {
    let message = err.to_string();
    let object_name = name.to_string();
    OwnedObject {
        name: object_name.clone(),
        store: StoreKind::Postgres,
        creator: Box::new(move |_| {
            Err(SetupError::CreationFailed {
                object: object_name.clone(),
                message: message.clone(),
            })
        }),
    }
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
    Ok(format!("\"{}\"", trimmed.replace('"', "\"\"")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::setup::{StandaloneSetup, StoreKind};

    #[test]
    fn standalone_setup_is_scoped() {
        let setup = GcodeStandaloneSetup::new("gcode_ci");
        assert_eq!(setup.namespace(), "gcode");
        assert_eq!(setup.schema(), "gcode_ci");

        let object_names: Vec<String> = setup
            .owned_objects()
            .into_iter()
            .map(|object| object.name)
            .collect();

        assert!(
            object_names
                .iter()
                .any(|name| name.contains("indexed_files"))
        );
        assert!(object_names.iter().any(|name| name.contains("symbols")));
        assert!(
            object_names
                .iter()
                .any(|name| name.contains("content_chunks"))
        );
        assert!(object_names.iter().any(|name| name.contains("sync_state")));
        assert!(object_names.iter().any(|name| name.contains("bm25")));

        let forbidden = ["config_store", ".gobby/project.json", "project_json"];
        for name in object_names {
            for forbidden_name in forbidden {
                assert!(
                    !name.contains(forbidden_name),
                    "standalone setup declared forbidden object {name}"
                );
            }
        }
    }

    #[test]
    fn standalone_setup_uses_gobby_core_contract() {
        fn assert_standalone_setup<T: StandaloneSetup>() {}
        assert_standalone_setup::<GcodeStandaloneSetup>();

        let setup = GcodeStandaloneSetup::new("gcode_contract");
        let objects = setup.owned_objects();
        assert!(
            objects
                .iter()
                .all(|object| object.store == StoreKind::Postgres)
        );
        assert!(
            objects
                .iter()
                .any(|object| object.name == "code_symbols table")
        );
        assert!(
            objects
                .iter()
                .any(|object| object.name == "code_symbols_search_bm25 index")
        );
    }
}
