use super::contracts::{
    DEFAULT_SCHEMA, INDEX_CONTRACTS, IndexContract, OVERWRITE_GUIDANCE, TABLE_CONTRACTS,
    TableContract, code_index_index_names, code_index_table_names,
};
use super::ddl::GcodeStandaloneSetup;
use super::identifiers::qualified_relation;
use super::types::{StandaloneSetupRequest, StandaloneSetupStatus};
use gobby_core::setup::{SetupContext, SetupError, SetupReport, StandaloneSetup};
use postgres::{Client, GenericClient};
use std::collections::HashSet;

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
    } else if let Err(err) = ensure_postgres_code_index_compatible(client, setup.schema()) {
        rollback_postgres_transaction(client, "code-index preflight rollback")?;
        return Err(err);
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
    for index in code_index_index_names() {
        statements.push(format!(
            "DROP INDEX IF EXISTS {};",
            qualified_relation(schema, index, "index")?
        ));
    }
    for table in code_index_table_names().rev() {
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
