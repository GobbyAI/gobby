use anyhow::{Context as _, bail};
use gobby_core::search::BM25_SCORE_REGPROCEDURE;
use postgres::Client;

fn current_schema_name(client: &mut Client) -> anyhow::Result<String> {
    client
        .query_one("SELECT current_schema()", &[])
        .context("failed to resolve current_schema")?
        .try_get(0)
        .context("failed to decode current_schema")
}

struct TableContract {
    name: &'static str,
    required_columns: &'static [&'static str],
}

const TABLE_CONTRACTS: &[TableContract] = &[
    TableContract {
        name: "code_indexed_projects",
        required_columns: &["id", "created_at", "updated_at"],
    },
    TableContract {
        name: "code_indexed_project_states",
        required_columns: &[
            "machine_id",
            "project_id",
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
            "vector_sync_attempted_at",
            "indexed_at",
            "last_referenced_at",
        ],
    },
    TableContract {
        name: "code_indexed_file_states",
        required_columns: &[
            "machine_id",
            "project_id",
            "file_path",
            "content_hash",
            "updated_at",
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
            "file_content_hash",
            "content_hash",
            "summary",
            "summary_attempted_at",
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
            "content_hash",
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
        required_columns: &[
            "id",
            "project_id",
            "source_file",
            "content_hash",
            "target_module",
        ],
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
            "content_hash",
            "line",
        ],
    },
];

const REQUIRED_TABLES: &[&str] = &[
    "code_indexed_projects",
    "code_indexed_project_states",
    "code_indexed_file_states",
    "code_indexed_files",
    "code_symbols",
    "code_content_chunks",
    "code_imports",
    "code_calls",
];

const REQUIRED_BM25_INDEXES: &[&str] = &["code_symbols_search_bm25", "code_content_search_bm25"];

const MIGRATION_HINT: &str = "Configure the Gobby PostgreSQL hub with the required code-index schema, `pg_search` extension, and BM25 indexes via `gdaemon apply`.";

/// Validate that the Gobby-owned PostgreSQL hub schema exists.
///
/// gcode does not create, alter, or drop hub tables. Schema ownership stays in
/// the Gobby baseline and migration chain.
pub fn validate_runtime_schema(client: &mut Client) -> anyhow::Result<()> {
    if !extension_exists(client, "pg_search")? {
        bail!("PostgreSQL hub is missing required extension `pg_search`. {MIGRATION_HINT}");
    }

    if !procedure_exists(client, BM25_SCORE_REGPROCEDURE)? {
        bail!(
            "PostgreSQL hub is missing required BM25 score function `{BM25_SCORE_REGPROCEDURE}`. {MIGRATION_HINT}"
        );
    }

    let schema = current_schema_name(client)?;
    let missing_tables = missing_relations(client, &schema, REQUIRED_TABLES)?;
    if !missing_tables.is_empty() {
        bail!(
            "PostgreSQL hub is missing required code-index tables: {}. {MIGRATION_HINT}",
            missing_tables.join(", ")
        );
    }

    let missing_columns = missing_table_columns(client, &schema, TABLE_CONTRACTS)?;
    if !missing_columns.is_empty() {
        bail!(
            "PostgreSQL hub is missing required code-index columns: {}. {MIGRATION_HINT}",
            missing_columns.join(", ")
        );
    }

    let missing_indexes = missing_relations(client, &schema, REQUIRED_BM25_INDEXES)?;
    if !missing_indexes.is_empty() {
        bail!(
            "PostgreSQL hub is missing required pg_search BM25 indexes: {}. {MIGRATION_HINT}",
            missing_indexes.join(", ")
        );
    }

    Ok(())
}

fn extension_exists(client: &mut Client, extension: &str) -> anyhow::Result<bool> {
    client
        .query_one(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = $1)",
            &[&extension],
        )
        .with_context(|| format!("failed to check PostgreSQL extension `{extension}`"))?
        .try_get(0)
        .context("failed to decode PostgreSQL extension check")
}

fn procedure_exists(client: &mut Client, procedure: &str) -> anyhow::Result<bool> {
    client
        .query_one("SELECT to_regprocedure($1) IS NOT NULL", &[&procedure])
        .with_context(|| format!("failed to check PostgreSQL procedure `{procedure}`"))?
        .try_get(0)
        .context("failed to decode PostgreSQL procedure check")
}

fn missing_relations(
    client: &mut Client,
    schema: &str,
    relations: &[&str],
) -> anyhow::Result<Vec<String>> {
    let mut missing = Vec::new();
    for relation in relations {
        let qualified = format!("{schema}.{relation}");
        let row = client
            .query_one("SELECT to_regclass($1) IS NOT NULL", &[&qualified])
            .with_context(|| format!("failed to check PostgreSQL relation `{qualified}`"))?;
        let exists: bool = row
            .try_get(0)
            .context("failed to decode PostgreSQL relation check")?;
        if !exists {
            missing.push((*relation).to_string());
        }
    }
    Ok(missing)
}

fn missing_table_columns(
    client: &mut Client,
    schema: &str,
    contracts: &[TableContract],
) -> anyhow::Result<Vec<String>> {
    let mut missing = Vec::new();
    for contract in contracts {
        let rows = client
            .query(
                "SELECT column_name
                 FROM information_schema.columns
                 WHERE table_schema = $1 AND table_name = $2",
                &[&schema, &contract.name],
            )
            .with_context(|| format!("query columns for table {}", contract.name))?;
        let existing = rows
            .into_iter()
            .map(|row| row.get::<_, String>("column_name"))
            .collect::<Vec<_>>();
        for column in contract.required_columns {
            if !existing.iter().any(|existing| existing == column) {
                missing.push(format!("{}.{}", contract.name, column));
            }
        }
    }
    Ok(missing)
}

#[cfg(test)]
fn required_relation_regclass_name(schema: &str, relation: &str) -> String {
    format!("{schema}.{relation}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn required_schema_contract_names_code_index_tables_and_bm25_indexes() {
        assert!(REQUIRED_TABLES.contains(&"code_symbols"));
        assert!(REQUIRED_TABLES.contains(&"code_content_chunks"));
        let indexed_files = TABLE_CONTRACTS
            .iter()
            .find(|contract| contract.name == "code_indexed_files")
            .expect("code_indexed_files contract");
        assert!(
            indexed_files
                .required_columns
                .contains(&"vector_sync_attempted_at")
        );
        let code_symbols = TABLE_CONTRACTS
            .iter()
            .find(|contract| contract.name == "code_symbols")
            .expect("code_symbols contract");
        assert!(
            code_symbols
                .required_columns
                .contains(&"summary_attempted_at")
        );
        assert!(REQUIRED_BM25_INDEXES.contains(&"code_symbols_search_bm25"));
        assert!(REQUIRED_BM25_INDEXES.contains(&"code_content_search_bm25"));
        assert_eq!(BM25_SCORE_REGPROCEDURE, "pdb.score(anyelement)");
    }

    mod serial_db {
        use super::*;

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires a PostgreSQL test database URL"
        )]
        #[serial_test::serial(serial_db)]
        fn validates_runtime_schema_when_postgres_test_dsn_is_set() {
            let database_url = crate::test_env::postgres_test_database_url("schema tests");

            let mut client = gobby_core::postgres::connect_readwrite(&database_url)
                .expect("connect test PostgreSQL hub");
            assert!(validate_runtime_schema(&mut client).is_ok());
        }
    }

    #[test]
    fn missing_schema_requires_setup() {
        assert!(
            MIGRATION_HINT.contains("gdaemon apply"),
            "missing runtime schema guidance must name gdaemon apply"
        );
    }

    #[test]
    fn relation_validation_qualifies_current_schema() {
        assert_eq!(
            required_relation_regclass_name("public", "code_symbols"),
            "public.code_symbols"
        );
    }
}
