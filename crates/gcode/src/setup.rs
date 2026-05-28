use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};
use postgres::{Client, GenericClient};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

const DEFAULT_SCHEMA: &str = "public";
const NAMESPACE: &str = "gcode";
const OVERWRITE_GUIDANCE: &str = "Rerun with `gcode setup --standalone --overwrite-code-index` to replace only gcode-owned code-index relations.";

const CODE_INDEX_TABLES: &[&str] = &[
    "code_indexed_projects",
    "code_indexed_files",
    "code_symbols",
    "code_content_chunks",
    "code_imports",
    "code_calls",
];

const CODE_INDEX_INDEXES: &[&str] = &[
    "idx_cif_project",
    "idx_cif_graph_synced",
    "idx_cif_vectors_synced",
    "idx_cs_project",
    "idx_cs_file",
    "idx_cs_name",
    "idx_cs_qualified",
    "idx_cs_kind",
    "idx_cs_parent",
    "idx_ccc_project",
    "idx_ccc_file",
    "idx_ci_file",
    "idx_cc_file",
    "idx_cc_caller",
    "idx_cc_target",
    "code_symbols_search_bm25",
    "code_content_search_bm25",
];

struct TableContract {
    name: &'static str,
    required_columns: &'static [&'static str],
}

struct IndexContract {
    name: &'static str,
    table: &'static str,
    method: &'static str,
}

const TABLE_CONTRACTS: &[TableContract] = &[
    TableContract {
        name: "code_indexed_projects",
        required_columns: &[
            "id",
            "root_path",
            "total_files",
            "total_symbols",
            "last_indexed_at",
            "index_duration_ms",
            "created_at",
            "updated_at",
        ],
    },
    TableContract {
        name: "code_indexed_files",
        required_columns: &[
            "id",
            "project_id",
            "file_path",
            "language",
            "content_hash",
            "symbol_count",
            "byte_size",
            "graph_synced",
            "vectors_synced",
            "graph_sync_attempted_at",
            "indexed_at",
        ],
    },
    TableContract {
        name: "code_symbols",
        required_columns: &[
            "id",
            "project_id",
            "file_path",
            "name",
            "qualified_name",
            "kind",
            "language",
            "byte_start",
            "byte_end",
            "line_start",
            "line_end",
            "signature",
            "docstring",
            "parent_symbol_id",
            "content_hash",
            "summary",
            "created_at",
            "updated_at",
        ],
    },
    TableContract {
        name: "code_content_chunks",
        required_columns: &[
            "id",
            "project_id",
            "file_path",
            "chunk_index",
            "line_start",
            "line_end",
            "content",
            "language",
            "created_at",
        ],
    },
    TableContract {
        name: "code_imports",
        required_columns: &["id", "project_id", "source_file", "target_module"],
    },
    TableContract {
        name: "code_calls",
        required_columns: &[
            "id",
            "project_id",
            "caller_symbol_id",
            "callee_symbol_id",
            "callee_name",
            "callee_target_kind",
            "callee_external_module",
            "file_path",
            "line",
        ],
    },
];

const INDEX_CONTRACTS: &[IndexContract] = &[
    IndexContract {
        name: "idx_cif_project",
        table: "code_indexed_files",
        method: "btree",
    },
    IndexContract {
        name: "idx_cif_graph_synced",
        table: "code_indexed_files",
        method: "btree",
    },
    IndexContract {
        name: "idx_cif_vectors_synced",
        table: "code_indexed_files",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_project",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_file",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_name",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_qualified",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_kind",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_parent",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_ccc_project",
        table: "code_content_chunks",
        method: "btree",
    },
    IndexContract {
        name: "idx_ccc_file",
        table: "code_content_chunks",
        method: "btree",
    },
    IndexContract {
        name: "idx_ci_file",
        table: "code_imports",
        method: "btree",
    },
    IndexContract {
        name: "idx_cc_file",
        table: "code_calls",
        method: "btree",
    },
    IndexContract {
        name: "idx_cc_caller",
        table: "code_calls",
        method: "btree",
    },
    IndexContract {
        name: "idx_cc_target",
        table: "code_calls",
        method: "btree",
    },
    IndexContract {
        name: "code_symbols_search_bm25",
        table: "code_symbols",
        method: "bm25",
    },
    IndexContract {
        name: "code_content_search_bm25",
        table: "code_content_chunks",
        method: "bm25",
    },
];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub standalone: bool,
    pub database_url: Option<String>,
    pub no_services: bool,
    pub overwrite_code_index: bool,
    pub schema: String,
    pub embedding_provider: Option<String>,
    pub embedding_api_base: Option<String>,
    pub embedding_model: Option<String>,
    pub embedding_vector_dim: Option<usize>,
    pub embedding_api_key_env: Option<String>,
    pub falkordb_host: Option<String>,
    pub falkordb_port: Option<u16>,
    pub falkordb_password: Option<String>,
    pub qdrant_url: Option<String>,
}

impl StandaloneSetupRequest {
    pub fn new(standalone: bool, database_url: Option<String>, schema: Option<String>) -> Self {
        Self {
            standalone,
            database_url,
            no_services: false,
            overwrite_code_index: false,
            schema: schema.unwrap_or_else(|| DEFAULT_SCHEMA.to_string()),
            embedding_provider: None,
            embedding_api_base: None,
            embedding_model: None,
            embedding_vector_dim: None,
            embedding_api_key_env: None,
            falkordb_host: None,
            falkordb_port: None,
            falkordb_password: None,
            qdrant_url: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneServicesStatus {
    pub provisioned: bool,
    pub compose_file: Option<String>,
    pub health_checks: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneEmbeddingStatus {
    pub provider: String,
    pub api_base: String,
    pub model: String,
    pub vector_dim: usize,
    pub api_key_env: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupStatus {
    pub namespace: String,
    pub schema: String,
    pub created: Vec<String>,
    pub skipped: Vec<String>,
    pub failed: Vec<(String, String)>,
    pub config_file: Option<String>,
    pub services: Option<StandaloneServicesStatus>,
    pub embedding: Option<StandaloneEmbeddingStatus>,
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

        vec![
            self.object(
                "pg_search extension",
                "CREATE EXTENSION IF NOT EXISTS pg_search;".to_string(),
            ),
            self.object(
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
                "idx_cif_project index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cif_project
                     ON {code_indexed_files}(project_id);"
                ),
            ),
            self.object(
                "idx_cif_graph_synced index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cif_graph_synced
                     ON {code_indexed_files}(project_id, graph_synced);"
                ),
            ),
            self.object(
                "idx_cif_vectors_synced index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cif_vectors_synced
                     ON {code_indexed_files}(project_id, vectors_synced);"
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
                        content_hash TEXT NOT NULL,
                        summary TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );"
                ),
            ),
            self.object(
                "idx_cs_project index",
                format!("CREATE INDEX IF NOT EXISTS idx_cs_project ON {code_symbols}(project_id);"),
            ),
            self.object(
                "idx_cs_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cs_file
                     ON {code_symbols}(project_id, file_path);"
                ),
            ),
            self.object(
                "idx_cs_name index",
                format!("CREATE INDEX IF NOT EXISTS idx_cs_name ON {code_symbols}(name);"),
            ),
            self.object(
                "idx_cs_qualified index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cs_qualified
                     ON {code_symbols}(qualified_name);"
                ),
            ),
            self.object(
                "idx_cs_kind index",
                format!("CREATE INDEX IF NOT EXISTS idx_cs_kind ON {code_symbols}(kind);"),
            ),
            self.object(
                "idx_cs_parent index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cs_parent
                     ON {code_symbols}(parent_symbol_id);"
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
                        language TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (project_id, file_path, chunk_index)
                    );"
                ),
            ),
            self.object(
                "idx_ccc_project index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_ccc_project
                     ON {code_content_chunks}(project_id);"
                ),
            ),
            self.object(
                "idx_ccc_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_ccc_file
                     ON {code_content_chunks}(project_id, file_path);"
                ),
            ),
            self.object(
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
            self.object(
                "idx_ci_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_ci_file
                     ON {code_imports}(project_id, source_file);"
                ),
            ),
            self.object(
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
            self.object(
                "idx_cc_file index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cc_file
                     ON {code_calls}(project_id, file_path);"
                ),
            ),
            self.object(
                "idx_cc_caller index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cc_caller
                     ON {code_calls}(project_id, caller_symbol_id);"
                ),
            ),
            self.object(
                "idx_cc_target index",
                format!(
                    "CREATE INDEX IF NOT EXISTS idx_cc_target
                     ON {code_calls}(project_id, callee_target_kind, callee_symbol_id, callee_name);"
                ),
            ),
            self.object(
                "code_symbols_search_bm25 index",
                format!(
                    "CREATE INDEX IF NOT EXISTS code_symbols_search_bm25
                     ON {code_symbols}
                     USING bm25 (id, name, qualified_name, signature, docstring, summary)
                     WITH (key_field = 'id');"
                ),
            ),
            self.object(
                "code_content_search_bm25 index",
                format!(
                    "CREATE INDEX IF NOT EXISTS code_content_search_bm25
                     ON {code_content_chunks}
                     USING bm25 (id, content)
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
                    break;
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
    validate_standalone_request(request)?;

    let setup = GcodeStandaloneSetup::new(request.schema.clone());
    begin_postgres_transaction(client)?;
    let mut reset_report = SetupReport::default();
    if request.overwrite_code_index {
        if let Err(err) = reset_postgres_code_index(client, setup.schema()) {
            reset_report
                .failed
                .push(("code-index overwrite reset".to_string(), err.to_string()));
            rollback_postgres_transaction(client, "code-index overwrite reset rollback")?;
            return Ok(standalone_setup_status(&setup, reset_report));
        }
    } else {
        if let Err(err) = ensure_postgres_code_index_compatible(client, setup.schema()) {
            rollback_postgres_transaction(client, "code-index preflight rollback")?;
            return Err(err);
        }
    }

    let report = {
        let mut ctx = SetupContext {
            pg: Some(client),
            falkor_config: None,
            qdrant_config: None,
            non_interactive: true,
        };
        setup.create(&mut ctx)?
    };
    if report.failed.is_empty() {
        commit_postgres_transaction(client)?;
    } else {
        rollback_postgres_transaction(client, "standalone setup rollback")?;
    }

    Ok(standalone_setup_status(&setup, report))
}

fn standalone_setup_status(
    setup: &GcodeStandaloneSetup,
    report: SetupReport,
) -> StandaloneSetupStatus {
    StandaloneSetupStatus {
        namespace: setup.namespace().to_string(),
        schema: setup.schema().to_string(),
        created: report.created,
        skipped: report.skipped,
        failed: report.failed,
        config_file: None,
        services: None,
        embedding: None,
    }
}

fn begin_postgres_transaction(client: &mut Client) -> Result<(), SetupError> {
    client
        .batch_execute("BEGIN")
        .map_err(|err| SetupError::CreationFailed {
            object: "standalone setup transaction".to_string(),
            message: err.to_string(),
        })
}

fn commit_postgres_transaction(client: &mut Client) -> Result<(), SetupError> {
    client
        .batch_execute("COMMIT")
        .map_err(|err| SetupError::CreationFailed {
            object: "standalone setup commit".to_string(),
            message: err.to_string(),
        })
}

fn rollback_postgres_transaction(client: &mut Client, object: &str) -> Result<(), SetupError> {
    client
        .batch_execute("ROLLBACK")
        .map_err(|err| SetupError::CreationFailed {
            object: object.to_string(),
            message: err.to_string(),
        })
}

pub(crate) fn ensure_postgres_code_index_compatible(
    client: &mut impl GenericClient,
    schema: &str,
) -> Result<(), SetupError> {
    let issues = incompatible_postgres_code_index_relations(client, schema)?;
    if issues.is_empty() {
        return Ok(());
    }

    Err(SetupError::CreationFailed {
        object: "code-index preflight".to_string(),
        message: format!(
            "existing code-index PostgreSQL state is incompatible: {}. {OVERWRITE_GUIDANCE}",
            issues.join("; ")
        ),
    })
}

pub(crate) fn reset_postgres_code_index(
    client: &mut impl GenericClient,
    schema: &str,
) -> Result<(), SetupError> {
    let sql = postgres_overwrite_reset_sql(schema)?;
    client
        .batch_execute(&sql)
        .map_err(|err| SetupError::CreationFailed {
            object: "code-index overwrite reset".to_string(),
            message: err.to_string(),
        })
}

pub(crate) fn postgres_overwrite_reset_sql(schema: &str) -> Result<String, SetupError> {
    let mut statements = Vec::new();
    for index in CODE_INDEX_INDEXES {
        statements.push(format!(
            "DROP INDEX IF EXISTS {};",
            qualified_relation(schema, index, "index")?
        ));
    }
    for table in CODE_INDEX_TABLES.iter().rev() {
        statements.push(format!(
            "DROP TABLE IF EXISTS {};",
            qualified_relation(schema, table, "table")?
        ));
    }
    Ok(statements.join("\n"))
}

fn incompatible_postgres_code_index_relations(
    client: &mut impl GenericClient,
    schema: &str,
) -> Result<Vec<String>, SetupError> {
    let mut issues = Vec::new();
    for contract in TABLE_CONTRACTS {
        inspect_table_contract(client, schema, contract, &mut issues)?;
    }
    for contract in INDEX_CONTRACTS {
        inspect_index_contract(client, schema, contract, &mut issues)?;
    }
    Ok(issues)
}

fn inspect_table_contract(
    client: &mut impl GenericClient,
    schema: &str,
    contract: &TableContract,
    issues: &mut Vec<String>,
) -> Result<(), SetupError> {
    let Some(kind) = relation_kind(client, schema, contract.name)? else {
        return Ok(());
    };
    if kind != "r" {
        issues.push(format!(
            "{} exists but is not an ordinary table",
            contract.name
        ));
        return Ok(());
    }

    let existing = table_columns(client, schema, contract.name)?;
    let missing = contract
        .required_columns
        .iter()
        .filter(|column| !existing.contains::<str>(column))
        .copied()
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        issues.push(format!(
            "{} is missing column(s): {}",
            contract.name,
            missing.join(", ")
        ));
    }
    Ok(())
}

fn inspect_index_contract(
    client: &mut impl GenericClient,
    schema: &str,
    contract: &IndexContract,
    issues: &mut Vec<String>,
) -> Result<(), SetupError> {
    let Some(index) = index_info(client, schema, contract.name)? else {
        return Ok(());
    };

    if index.relkind != "i" && index.relkind != "I" {
        issues.push(format!("{} exists but is not an index", contract.name));
        return Ok(());
    }
    if index.table_name.as_deref() != Some(contract.table) {
        issues.push(format!(
            "{} is attached to {}, expected {}",
            contract.name,
            index.table_name.as_deref().unwrap_or("<unknown>"),
            contract.table
        ));
    }
    if index.method.as_deref() != Some(contract.method) {
        issues.push(format!(
            "{} uses access method {}, expected {}",
            contract.name,
            index.method.as_deref().unwrap_or("<unknown>"),
            contract.method
        ));
    }
    Ok(())
}

fn relation_kind(
    client: &mut impl GenericClient,
    schema: &str,
    relation: &str,
) -> Result<Option<String>, SetupError> {
    let row = client
        .query_opt(
            "SELECT c.relkind::TEXT
             FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1 AND c.relname = $2",
            &[&schema, &relation],
        )
        .map_err(|err| SetupError::CreationFailed {
            object: format!("{relation} preflight"),
            message: err.to_string(),
        })?;
    Ok(row.map(|row| row.get(0)))
}

fn table_columns(
    client: &mut impl GenericClient,
    schema: &str,
    table: &str,
) -> Result<HashSet<String>, SetupError> {
    let rows = client
        .query(
            "SELECT a.attname
             FROM pg_attribute a
             JOIN pg_class c ON c.oid = a.attrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1
               AND c.relname = $2
               AND a.attnum > 0
               AND NOT a.attisdropped",
            &[&schema, &table],
        )
        .map_err(|err| SetupError::CreationFailed {
            object: format!("{table} preflight"),
            message: err.to_string(),
        })?;
    Ok(rows.into_iter().map(|row| row.get(0)).collect())
}

struct ExistingIndexInfo {
    relkind: String,
    table_name: Option<String>,
    method: Option<String>,
}

fn index_info(
    client: &mut impl GenericClient,
    schema: &str,
    index: &str,
) -> Result<Option<ExistingIndexInfo>, SetupError> {
    let row = client
        .query_opt(
            "SELECT c.relkind::TEXT,
                    table_class.relname::TEXT AS table_name,
                    am.amname::TEXT AS method
             FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
             LEFT JOIN pg_index idx ON idx.indexrelid = c.oid
             LEFT JOIN pg_class table_class ON table_class.oid = idx.indrelid
             LEFT JOIN pg_am am ON am.oid = c.relam
             WHERE n.nspname = $1 AND c.relname = $2",
            &[&schema, &index],
        )
        .map_err(|err| SetupError::CreationFailed {
            object: format!("{index} preflight"),
            message: err.to_string(),
        })?;

    Ok(row.map(|row| ExistingIndexInfo {
        relkind: row.get(0),
        table_name: row.get(1),
        method: row.get(2),
    }))
}

pub fn validate_standalone_request(request: &StandaloneSetupRequest) -> Result<(), SetupError> {
    if !request.standalone {
        return Err(SetupError::AttachedModeRefused);
    }
    if request.schema != DEFAULT_SCHEMA {
        return Err(SetupError::CreationFailed {
            object: "schema".to_string(),
            message: "standalone code-index schema must be `public` for daemon adoption"
                .to_string(),
        });
    }
    Ok(())
}

fn qualified_relation(schema: &str, relation: &str, label: &str) -> Result<String, SetupError> {
    Ok(format!(
        "{}.{}",
        quote_identifier(schema, "schema")?,
        quote_identifier(relation, label)?
    ))
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
    use postgres::NoTls;

    #[test]
    fn standalone_setup_declares_public_daemon_code_index_subset() {
        let setup = GcodeStandaloneSetup::new("public");
        assert_eq!(setup.namespace(), "gcode");
        assert_eq!(setup.schema(), "public");

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
        assert!(object_names.iter().any(|name| name.contains("idx_cif")));
        assert!(object_names.iter().any(|name| name.contains("bm25")));

        let forbidden = [
            "config_store",
            "schema_migrations",
            "secrets",
            ".gobby/project.json",
            "project_json",
            "code_graph_sync_state",
            "code_vector_sync_state",
        ];
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

        let setup = GcodeStandaloneSetup::new("public");
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
        assert!(
            objects
                .iter()
                .any(|object| object.name == "pg_search extension")
        );
    }

    #[test]
    fn standalone_setup_rejects_non_public_schema() {
        let request = StandaloneSetupRequest::new(
            true,
            Some("postgresql://localhost/gcode".to_string()),
            Some("gcode_ci".to_string()),
        );
        let err = validate_standalone_request(&request).expect_err("non-public schema fails");
        assert!(err.to_string().contains("public"));
    }

    #[test]
    fn overwrite_reset_sql_is_allowlisted() {
        let sql = postgres_overwrite_reset_sql("public").expect("reset SQL");

        for table in CODE_INDEX_TABLES {
            assert!(
                sql.contains(&format!("DROP TABLE IF EXISTS \"public\".\"{table}\";")),
                "{sql}"
            );
        }
        for index in CODE_INDEX_INDEXES {
            assert!(
                sql.contains(&format!("DROP INDEX IF EXISTS \"public\".\"{index}\";")),
                "{sql}"
            );
        }

        for forbidden in [
            "config_store",
            "schema_migrations",
            "secrets",
            "tasks",
            "sessions",
            "memory",
            ".gobby/project.json",
        ] {
            assert!(!sql.contains(forbidden), "{sql}");
        }
        assert!(!sql.contains("CASCADE"), "{sql}");
        assert!(!sql.contains("DROP DATABASE"), "{sql}");
        assert!(!sql.contains("DROP SCHEMA"), "{sql}");
    }

    #[test]
    fn overwrite_guidance_names_flag() {
        let request = StandaloneSetupRequest::new(true, None, None);
        assert!(!request.overwrite_code_index);
        assert!(OVERWRITE_GUIDANCE.contains("--overwrite-code-index"));
    }

    #[test]
    #[serial_test::serial]
    fn overwrite_recreates_incompatible_code_index_and_preserves_sentinel_table() {
        let Ok(database_url) = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL") else {
            return;
        };
        let mut client =
            Client::connect(&database_url, NoTls).expect("connect test PostgreSQL hub");
        cleanup_code_index_relations(&mut client);
        client
            .batch_execute(
                "CREATE TABLE public.code_symbols (id TEXT PRIMARY KEY);
                 CREATE TABLE IF NOT EXISTS public.gobby_owned_sentinel (
                     key TEXT PRIMARY KEY,
                     value TEXT NOT NULL
                 );
                 INSERT INTO public.gobby_owned_sentinel (key, value)
                 VALUES ('gcode-overwrite-sentinel', 'keep-me')
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
            )
            .expect("seed incompatible code index and sentinel");

        let request = StandaloneSetupRequest::new(true, Some(database_url.clone()), None);
        let err = run_standalone_setup(&request, &mut client)
            .expect_err("incompatible setup fails without overwrite");
        assert!(err.to_string().contains("--overwrite-code-index"));

        let mut overwrite = StandaloneSetupRequest::new(true, Some(database_url), None);
        overwrite.overwrite_code_index = true;
        run_standalone_setup(&overwrite, &mut client).expect("overwrite setup succeeds");

        let has_project_id: bool = client
            .query_one(
                "SELECT EXISTS(
                    SELECT 1
                    FROM pg_attribute
                    WHERE attrelid = 'public.code_symbols'::regclass
                      AND attname = 'project_id'
                      AND attnum > 0
                      AND NOT attisdropped
                )",
                &[],
            )
            .expect("check recreated code_symbols")
            .get(0);
        assert!(has_project_id);

        let sentinel: String = client
            .query_one(
                "SELECT value FROM public.gobby_owned_sentinel WHERE key = 'gcode-overwrite-sentinel'",
                &[],
            )
            .expect("read sentinel")
            .get(0);
        assert_eq!(sentinel, "keep-me");

        cleanup_code_index_relations(&mut client);
        client
            .batch_execute(
                "DELETE FROM public.gobby_owned_sentinel WHERE key = 'gcode-overwrite-sentinel';
                 DROP TABLE IF EXISTS public.gobby_owned_sentinel;",
            )
            .expect("cleanup sentinel");
    }

    fn cleanup_code_index_relations(client: &mut Client) {
        let sql = postgres_overwrite_reset_sql("public").expect("reset SQL");
        client
            .batch_execute(&sql)
            .expect("cleanup code index objects");
    }
}
