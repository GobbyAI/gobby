//! Read-only schema planning: what `apply` would do, without doing it.
//!
//! `gobby restart` and `gobby cutover` are stop-then-start with no way back, so
//! both must prove the start half before anything is stopped or promoted. `plan`
//! answers "would `schema apply` succeed against this database right now" using
//! the same lineage validation and pending resolution `apply` performs. It never
//! creates the schema and never takes the apply advisory lock, so it is safe to
//! run against a live hub and fails closed if it observes a mid-apply state.

use std::collections::BTreeSet;

use postgres::Client;

use super::{
    BASELINE_CHECKSUM, BASELINE_VERSION, BaselineState, EmbeddedMigration, PRIOR_RECEIPT_CHECKSUMS,
    SchemaError, SchemaRunner, VerifiedBackupManifest, baseline_filename, classify_baseline_state,
    has_directive, is_prior_baseline_receipt, latest_version, qualified_name, read_schema_head,
    read_source_identity, require_pg_search, set_search_path, verify_adopted_columns,
    verify_embedded_assets,
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
    /// Validate the database lineage exactly as `apply` does, writing nothing.
    ///
    /// Returns the current database head and a lineage that is never
    /// `BaselineState::CorruptPartial`: that arm is the refusal below.
    pub(super) fn validate_lineage(
        &mut self,
        backup: Option<&VerifiedBackupManifest>,
    ) -> Result<(i32, BaselineState), SchemaError> {
        let code_head = latest_version(self.migrations);
        let database_head = read_schema_head(self.client, &self.schema)?;
        if database_head > code_head {
            return Err(SchemaError::Unsupported(format!(
                "database schema v{database_head} is newer than this runner (v{code_head})"
            )));
        }
        if let Some(backup) = backup {
            let current_identity = read_source_identity(self.client)?;
            if backup.manifest().source_identity != current_identity {
                return Err(SchemaError::Unsupported(
                    "verified backup source identity does not match the current database"
                        .to_owned(),
                ));
            }
            if backup.manifest().backup_starting_head != database_head {
                return Err(SchemaError::Unsupported(format!(
                    "verified backup starts at schema v{}, current database is v{database_head}",
                    backup.manifest().backup_starting_head
                )));
            }
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
        let (database_head, state) = self.validate_lineage(None)?;
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

        let pending =
            resolve_pending_migrations(self.client, &self.schema, self.migrations, state, false)?;
        Ok(PlanReport {
            database_head,
            code_head,
            baseline_pending: false,
            pending_versions: pending.iter().map(|migration| migration.version).collect(),
        })
    }
}

/// Validate every receipt and return the migrations an apply would run, in order.
///
/// Writes nothing: receipt validation and the destructive-authorization checks
/// only. `apply_pending_migrations` executes the list this returns.
pub(super) fn resolve_pending_migrations<'m>(
    client: &mut Client,
    schema: &str,
    migrations: &'m [EmbeddedMigration],
    lineage: BaselineState,
    destructive_authorized: bool,
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
    let stamp_destructive = lineage.stamps_destructive_migrations();
    for migration in &pending {
        let destructive = has_directive(migration.sql, "-- gobby:destructive");
        let non_transactional = has_directive(migration.sql, "-- gobby:non-transactional");
        if destructive && !destructive_authorized && !stamp_destructive {
            return Err(SchemaError::Unsupported(format!(
                "migration {} is destructive; a verified hub backup is required",
                migration.filename
            )));
        }
        if non_transactional && destructive {
            return Err(SchemaError::Unsupported(format!(
                "destructive migration {} cannot be non-transactional",
                migration.filename
            )));
        }
    }
    Ok(pending)
}
