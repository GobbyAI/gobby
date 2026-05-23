use anyhow::{Context as _, bail};
use postgres::Client;

const REQUIRED_TABLES: &[&str] = &[
    "code_indexed_projects",
    "code_indexed_files",
    "code_symbols",
    "code_content_chunks",
    "code_imports",
    "code_calls",
];

const REQUIRED_BM25_INDEXES: &[&str] = &["code_symbols_search_bm25", "code_content_search_bm25"];

const MIGRATION_HINT: &str = "Configure the Gobby PostgreSQL hub with the required code-index schema, `pg_search` extension, and BM25 indexes.";

/// Validate that the Gobby-owned PostgreSQL hub schema exists.
///
/// gcode does not create, alter, or drop hub tables. Schema ownership stays in
/// the Gobby baseline and migration chain.
pub fn validate_runtime_schema(client: &mut Client) -> anyhow::Result<()> {
    if !extension_exists(client, "pg_search")? {
        bail!("PostgreSQL hub is missing required extension `pg_search`. {MIGRATION_HINT}");
    }

    let missing_tables = missing_relations(client, REQUIRED_TABLES)?;
    if !missing_tables.is_empty() {
        bail!(
            "PostgreSQL hub is missing required code-index tables: {}. {MIGRATION_HINT}",
            missing_tables.join(", ")
        );
    }

    let missing_indexes = missing_relations(client, REQUIRED_BM25_INDEXES)?;
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

fn missing_relations(client: &mut Client, relations: &[&str]) -> anyhow::Result<Vec<String>> {
    let mut missing = Vec::new();
    for relation in relations {
        let row = client
            .query_one("SELECT to_regclass($1) IS NOT NULL", &[relation])
            .with_context(|| format!("failed to check PostgreSQL relation `{relation}`"))?;
        let exists: bool = row
            .try_get(0)
            .context("failed to decode PostgreSQL relation check")?;
        if !exists {
            missing.push((*relation).to_string());
        }
    }
    Ok(missing)
}

#[cfg(test)]
mod tests {
    use super::*;
    use postgres::NoTls;

    #[test]
    fn required_schema_contract_names_code_index_tables_and_bm25_indexes() {
        assert!(REQUIRED_TABLES.contains(&"code_symbols"));
        assert!(REQUIRED_TABLES.contains(&"code_content_chunks"));
        assert!(REQUIRED_BM25_INDEXES.contains(&"code_symbols_search_bm25"));
        assert!(REQUIRED_BM25_INDEXES.contains(&"code_content_search_bm25"));
    }

    #[test]
    fn validates_runtime_schema_when_postgres_test_dsn_is_set() {
        let Ok(database_url) = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL") else {
            return;
        };

        let mut client =
            Client::connect(&database_url, NoTls).expect("connect test PostgreSQL hub");
        validate_runtime_schema(&mut client).expect("validate test PostgreSQL hub schema");
    }
}
