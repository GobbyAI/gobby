//! Read-only schema planning: what `apply` would do, without doing it.
//!
//! `gobby restart` and `gobby cutover` are stop-then-start with no way back, so
//! both must prove the start half before anything is stopped or promoted. `plan`
//! lists the work using the same lineage validation and pending resolution
//! `apply` performs. Data-dependent failures need the rollback-only `preflight`.
//! `plan` never creates the schema or takes the apply advisory lock, so it is safe to
//! run against a live hub and fails closed if it observes a mid-apply state.

use std::collections::BTreeSet;

use postgres::Client;

use crate::schema::sql_splitter::leading_keyword;

use super::{
    BASELINE_CHECKSUM, BASELINE_VERSION, BaselineState, EmbeddedMigration, PRIOR_RECEIPT_CHECKSUMS,
    SchemaError, SchemaRunner, acquire_apply_lock, baseline_filename, classify_baseline_state,
    has_directive, insert_receipt, is_prior_baseline_receipt, latest_version, qualified_name,
    read_schema_head, release_apply_lock, render_sql_for_schema, require_pg_search,
    set_search_path, split_sql_statements, verify_adopted_columns, verify_embedded_assets,
};

/// What one `SchemaRunner::apply` would do against the current database.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlanReport {
    pub database_head: i32,
    pub code_head: i32,
    pub baseline_pending: bool,
    pub pending_versions: Vec<i32>,
}

impl SchemaRunner<'_> {
    /// Prove pending migrations against current rows, then roll everything back.
    ///
    /// Uses apply's lock, pending resolution, SQL rendering and receipt writes.
    /// Rows, schema and receipts are unchanged; PostgreSQL sequence values may
    /// be consumed even though no data commits. Non-transactional migrations,
    /// transaction control and baseline initialization are refused.
    pub fn preflight(&mut self) -> Result<PlanReport, SchemaError> {
        acquire_apply_lock(self.client)?;
        let result = self.preflight_locked();
        let unlock = release_apply_lock(self.client);
        match result {
            Err(error) => {
                let _ = unlock;
                Err(error)
            }
            Ok(report) => {
                unlock?;
                Ok(report)
            }
        }
    }

    fn preflight_locked(&mut self) -> Result<PlanReport, SchemaError> {
        let report = self.plan()?;
        if report.baseline_pending {
            return Err(SchemaError::Unsupported(format!(
                "baseline@{BASELINE_VERSION} is pending; initialize the hub before restart or cutover"
            )));
        }
        let pending = resolve_pending_migrations(self.client, &self.schema, self.migrations)?;
        for migration in &pending {
            if has_directive(migration.sql, "-- gobby:non-transactional") {
                return Err(SchemaError::Unsupported(format!(
                    "migration {} is non-transactional and cannot be rollback-proven",
                    migration.filename
                )));
            }
            for statement in split_sql_statements(migration.sql)? {
                let keyword = leading_keyword(&statement)?.to_ascii_uppercase();
                if [
                    "BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "PREPARE",
                ]
                .contains(&keyword.as_str())
                {
                    return Err(SchemaError::Unsupported(format!(
                        "migration {} contains transaction control and cannot be rollback-proven: {statement}",
                        migration.filename
                    )));
                }
            }
        }
        let receipt_table = qualified_name(&self.schema, "schema_migrations")?;
        let mut transaction = self.client.transaction()?;
        let proof = (|| -> Result<(), SchemaError> {
            // A proof should refuse promptly instead of waiting on the running
            // daemon's rows or retaining DDL locks for an unbounded statement.
            transaction.batch_execute(
                "SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '60s'",
            )?;
            for migration in pending {
                for statement in
                    split_sql_statements(&render_sql_for_schema(migration.sql, &self.schema))?
                {
                    transaction
                        .batch_execute(&statement)
                        .map_err(|source| migration_failure(migration, &statement, source))?;
                }
                insert_receipt(&mut transaction, &receipt_table, migration).map_err(|source| {
                    migration_failure(migration, "INSERT schema_migrations receipt", source)
                })?;
                // Apply commits each migration. Force its deferred checks before
                // a successor could hide a failure that commit would encounter.
                transaction
                    .batch_execute("SET CONSTRAINTS ALL IMMEDIATE")
                    .map_err(|source| {
                        migration_failure(migration, "SET CONSTRAINTS ALL IMMEDIATE", source)
                    })?;
            }
            Ok(())
        })();
        // Explicitly roll back on both paths, preserving the migration error if
        // the connection has also failed. Transaction's Drop is the final guard.
        let rollback = transaction.rollback();
        proof?;
        rollback?;
        Ok(report)
    }

    /// Validate the database lineage exactly as `apply` does, writing nothing.
    ///
    /// Returns the current database head and a lineage that is never
    /// `BaselineState::CorruptPartial`: that arm is the refusal below.
    pub(super) fn validate_lineage(&mut self) -> Result<(i32, BaselineState), SchemaError> {
        let code_head = latest_version(self.migrations);
        let database_head = read_schema_head(self.client, &self.schema)?;
        if database_head > code_head {
            return Err(SchemaError::Unsupported(format!(
                "database schema v{database_head} is newer than this runner (v{code_head})"
            )));
        }

        let state = classify_baseline_state(self.client, &self.schema)?;
        if matches!(state, BaselineState::CorruptPartial) {
            return Err(SchemaError::Unsupported(
                "unrecognized schema lineage; recreate from a verified backup".to_owned(),
            ));
        }
        Ok((database_head, state))
    }

    /// Report what an apply would do without writing anything.
    ///
    /// Deliberately performs lineage validation and pending resolution only. It
    /// never verifies the database identity, because a database head that differs
    /// from the embedded head is the normal state of every migration-owing restart
    /// and of every cutover — the exact cases this command exists to clear.
    pub fn plan(&mut self) -> Result<PlanReport, SchemaError> {
        verify_embedded_assets(self.migrations)?;
        set_search_path(self.client, &self.schema)?;
        let (database_head, state) = self.validate_lineage()?;
        let code_head = latest_version(self.migrations);

        if state.is_fresh_lineage() {
            // `schema_migrations` does not exist yet, so every embedded asset is
            // pending and there are no receipts to resolve.
            require_pg_search(self.client)?;
            verify_adopted_columns(self.client, &self.schema, state)?;
            return Ok(PlanReport {
                database_head,
                code_head,
                baseline_pending: true,
                pending_versions: self
                    .migrations
                    .iter()
                    .map(|migration| migration.version)
                    .collect(),
            });
        }

        let pending = resolve_pending_migrations(self.client, &self.schema, self.migrations)?;
        Ok(PlanReport {
            database_head,
            code_head,
            baseline_pending: false,
            pending_versions: pending.iter().map(|migration| migration.version).collect(),
        })
    }
}

fn migration_failure(
    migration: &EmbeddedMigration,
    statement: &str,
    source: postgres::Error,
) -> SchemaError {
    let detail = source
        .as_db_error()
        .map_or_else(|| source.to_string(), ToString::to_string);
    SchemaError::Migration {
        version: migration.version,
        filename: migration.filename,
        statement: statement.to_owned(),
        detail,
        source,
    }
}

/// Validate every receipt and return the migrations an apply would run, in order.
///
/// Writes nothing: receipt validation and the directive checks only.
/// `apply_pending_migrations` executes the list this returns.
pub(super) fn resolve_pending_migrations<'m>(
    client: &mut Client,
    schema: &str,
    migrations: &'m [EmbeddedMigration],
) -> Result<Vec<&'m EmbeddedMigration>, SchemaError> {
    let table = qualified_name(schema, "schema_migrations")?;
    let rows = client.query(
        &format!("SELECT version, filename, checksum FROM {table} ORDER BY version"),
        &[],
    )?;
    let mut applied = BTreeSet::new();
    for row in rows {
        let version: i32 = row.get(0);
        let filename = row.get::<_, Option<String>>(1).unwrap_or_default();
        let checksum = row.get::<_, Option<String>>(2).unwrap_or_default();
        if is_prior_baseline_receipt(version, &filename, &checksum) {
            applied.insert(BASELINE_VERSION);
            continue;
        }
        let expected = if version == BASELINE_VERSION {
            Some((baseline_filename(), BASELINE_CHECKSUM))
        } else {
            migrations
                .iter()
                .find(|migration| migration.version == version)
                .map(|migration| (migration.filename.to_owned(), migration.checksum))
        };
        let Some((expected_filename, expected_checksum)) = expected else {
            return Err(SchemaError::Unsupported(format!(
                "receipt v{version} has no matching embedded schema asset"
            )));
        };
        let prior_ok = PRIOR_RECEIPT_CHECKSUMS
            .iter()
            .any(|(prior_version, prior_checksum)| {
                *prior_version == version && *prior_checksum == checksum
            });
        if filename != expected_filename || (checksum != expected_checksum && !prior_ok) {
            return Err(SchemaError::Unsupported(format!(
                "receipt mismatch for schema asset v{version}"
            )));
        }
        applied.insert(version);
    }

    let pending: Vec<&EmbeddedMigration> = migrations
        .iter()
        .filter(|migration| !applied.contains(&migration.version))
        .collect();
    for migration in &pending {
        let destructive = has_directive(migration.sql, "-- gobby:destructive");
        let non_transactional = has_directive(migration.sql, "-- gobby:non-transactional");
        if non_transactional && destructive {
            return Err(SchemaError::Unsupported(format!(
                "destructive migration {} cannot be non-transactional",
                migration.filename
            )));
        }
    }
    Ok(pending)
}
