use std::borrow::Cow;
use std::collections::BTreeSet;
use std::thread;
use std::time::{Duration, Instant};

use postgres::{Client, GenericClient};

use super::assets::{
    BASELINE_CHECKSUM, BASELINE_SQL, BASELINE_VERSION, EmbeddedMigration, MIGRATIONS,
    PRIOR_RECEIPT_CHECKSUMS, TOOL_CHAT_OVERLAY_PREDECESSOR_CHECKSUM,
    WORKTREE_PRE_OVERLAY_BASELINE_CHECKSUM, sha256_hex,
};
use super::baseline_refresh::{RefreshMode, baseline_refresh_statement_for_mode};
use super::error::SchemaError;
use super::gate::{SourceIdentity, VerifiedBackupManifest};
use super::sql_splitter::split_sql_statements;
use super::verify::{VerificationReport, qualified_name, validate_identifier, verify_schema};

#[path = "runner_adoption.rs"]
mod runner_adoption;
use runner_adoption::{
    GCORE_CODE_INDEX_CORE_TABLES, GCORE_CODE_INDEX_TABLES, adopted_column_contracts,
};

pub(crate) const ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM: &str =
    "855576453641152d2ef9199dc418fcc3dd2ad69e78eff924b05a7b3b122cf398";
pub(crate) const PREDECESSOR_BASELINE_CHECKSUM: &str =
    "f4cbda62b6ab0cdf426c581693f26373968b02578387abf843baab4e600c3aa0";
pub(crate) const PARENT_BASELINE_CHECKSUM: &str =
    "2d3f79a8a6aef0426d6604956d3edc2d8c5e89e01b6bced2643a88e55da342e5";
pub(crate) const WORKTREE_BASELINE_CHECKSUM: &str =
    "648d01b53b9f77338894de16557f4c7c75e8c1bbca4146d263bd690723955440";

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ApplyReport {
    pub baseline_applied: bool,
    pub migrations_applied: usize,
}

pub struct SchemaRunner<'a> {
    client: &'a mut Client,
    schema: String,
    migrations: &'static [EmbeddedMigration],
}

impl std::fmt::Debug for SchemaRunner<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SchemaRunner")
            .field("schema", &self.schema)
            .finish_non_exhaustive()
    }
}

impl<'a> SchemaRunner<'a> {
    pub fn new(client: &'a mut Client, schema: impl Into<String>) -> Result<Self, SchemaError> {
        let schema = schema.into();
        if !is_identifier(&schema) {
            return Err(SchemaError::InvalidSchema(schema));
        }
        Ok(Self {
            client,
            schema,
            migrations: MIGRATIONS,
        })
    }

    pub fn schema(&self) -> &str {
        &self.schema
    }

    pub fn client(&mut self) -> &mut Client {
        self.client
    }

    pub fn apply(&mut self) -> Result<ApplyReport, SchemaError> {
        self.apply_internal(None)
    }

    pub fn apply_with_backup(
        &mut self,
        backup: &VerifiedBackupManifest,
    ) -> Result<ApplyReport, SchemaError> {
        self.apply_internal(Some(backup))
    }

    pub fn verify(&mut self) -> Result<VerificationReport, SchemaError> {
        verify_schema(self.client, &self.schema)
    }

    #[cfg(test)]
    pub(crate) fn with_migrations_for_test(
        client: &'a mut Client,
        schema: &str,
        migrations: &'static [EmbeddedMigration],
    ) -> Result<Self, SchemaError> {
        let mut runner = Self::new(client, schema)?;
        runner.migrations = migrations;
        Ok(runner)
    }

    fn apply_internal(
        &mut self,
        backup: Option<&VerifiedBackupManifest>,
    ) -> Result<ApplyReport, SchemaError> {
        verify_embedded_assets(self.migrations)?;
        ensure_schema(self.client, &self.schema)?;
        set_search_path(self.client, &self.schema)?;
        acquire_apply_lock(self.client)?;
        let result = self.apply_locked(backup);
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

    fn apply_locked(
        &mut self,
        backup: Option<&VerifiedBackupManifest>,
    ) -> Result<ApplyReport, SchemaError> {
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
        let baseline_applied = match state {
            BaselineState::AlreadyBaselined => false,
            BaselineState::Fresh
            | BaselineState::FreshWithInstallInfra
            | BaselineState::GcoreCodeIndex
            | BaselineState::GwikiStandalone
            | BaselineState::PredecessorBaseline
            | BaselineState::ParentBaseline
            | BaselineState::WorktreeBaseline => {
                require_pg_search(self.client)?;
                verify_adopted_columns(self.client, &self.schema, state)?;
                apply_baseline(self.client, &self.schema, state)?;
                true
            }
            BaselineState::CorruptPartial => {
                return Err(SchemaError::Unsupported(
                    "unrecognized or pre-375 schema lineage; recreate from a verified backup"
                        .to_owned(),
                ));
            }
            BaselineState::AccountIdentityPredecessor => {
                return Err(SchemaError::Unsupported(
                    "schema baseline requires account-identity-cutover; run 'gobby hub-maintenance run account-identity-cutover'"
                        .to_owned(),
                ));
            }
        };
        let migrations_applied = apply_pending_migrations(
            self.client,
            &self.schema,
            self.migrations,
            state,
            backup.is_some(),
        )?;
        Ok(ApplyReport {
            baseline_applied,
            migrations_applied,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BaselineState {
    Fresh,
    FreshWithInstallInfra,
    GcoreCodeIndex,
    GwikiStandalone,
    AlreadyBaselined,
    AccountIdentityPredecessor,
    PredecessorBaseline,
    ParentBaseline,
    WorktreeBaseline,
    CorruptPartial,
}

impl BaselineState {
    fn is_fresh_lineage(self) -> bool {
        matches!(
            self,
            Self::Fresh
                | Self::FreshWithInstallInfra
                | Self::GcoreCodeIndex
                | Self::GwikiStandalone
        )
    }

    fn stamps_destructive_migrations(self) -> bool {
        self.is_fresh_lineage() || matches!(self, Self::ParentBaseline)
    }

    fn updates_baseline_receipt(self) -> bool {
        matches!(
            self,
            Self::PredecessorBaseline | Self::ParentBaseline | Self::WorktreeBaseline
        )
    }

    fn refresh_mode(self) -> Option<RefreshMode> {
        match self {
            Self::PredecessorBaseline => Some(RefreshMode::TypedDomainAndRuntime),
            Self::ParentBaseline => Some(RefreshMode::RuntimeOnly),
            Self::WorktreeBaseline => Some(RefreshMode::TypedDomainOnly),
            _ => None,
        }
    }
}

const GWIKI_TABLES: [&str; 3] = ["gwiki_chunks", "gwiki_documents", "gwiki_sources"];

const APPLY_LOCK_POLL: Duration = Duration::from_millis(100);
const APPLY_LOCK_TIMEOUT: Duration = Duration::from_secs(600);

/// Serialize every schema apply in the database on one session-level lock.
///
/// The baseline and migrations create cluster-global roles and the shared
/// `gobby_agent_auth` objects, so applies into different schemas still race on
/// the same catalog tuples. Waiters poll `pg_try_advisory_lock` instead of
/// blocking in `pg_advisory_lock`: a session parked inside that call holds an
/// open transaction, and the holder's `CREATE INDEX CONCURRENTLY` waits for
/// every open transaction — a deadlock PostgreSQL resolves by killing one side.
fn acquire_apply_lock(client: &mut Client) -> Result<(), SchemaError> {
    let deadline = Instant::now() + APPLY_LOCK_TIMEOUT;
    loop {
        let acquired: bool = client
            .query_one(
                "SELECT pg_try_advisory_lock(hashtext('postgres_migrations_apply'), 0)",
                &[],
            )?
            .get(0);
        if acquired {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(SchemaError::ApplyLock(format!(
                "another schema runner held the database apply lock for more than {}s",
                APPLY_LOCK_TIMEOUT.as_secs()
            )));
        }
        thread::sleep(APPLY_LOCK_POLL);
    }
}

fn release_apply_lock(client: &mut Client) -> Result<(), SchemaError> {
    client.query_one(
        "SELECT pg_advisory_unlock(hashtext('postgres_migrations_apply'), 0)",
        &[],
    )?;
    Ok(())
}

fn ensure_schema(client: &mut Client, schema: &str) -> Result<(), SchemaError> {
    validate_identifier(schema)?;
    client.batch_execute(&format!("CREATE SCHEMA IF NOT EXISTS \"{schema}\""))?;
    Ok(())
}

fn set_search_path(client: &mut Client, schema: &str) -> Result<(), SchemaError> {
    validate_identifier(schema)?;
    client.batch_execute(&format!("SET search_path TO \"{schema}\", pg_catalog"))?;
    Ok(())
}

fn verify_embedded_assets(migrations: &[EmbeddedMigration]) -> Result<(), SchemaError> {
    let baseline_checksum = sha256_hex(BASELINE_SQL.as_bytes());
    if baseline_checksum != BASELINE_CHECKSUM {
        return Err(SchemaError::Unsupported(format!(
            "embedded baseline checksum mismatch: expected {BASELINE_CHECKSUM}, got {baseline_checksum}"
        )));
    }
    for (expected_version, migration) in (BASELINE_VERSION + 1..).zip(migrations.iter()) {
        if migration.version != expected_version {
            return Err(SchemaError::Unsupported(format!(
                "migration chain is not contiguous: expected v{expected_version}, got v{}",
                migration.version
            )));
        }
        let checksum = sha256_hex(migration.sql.as_bytes());
        if checksum != migration.checksum {
            return Err(SchemaError::Unsupported(format!(
                "embedded migration checksum mismatch for {}",
                migration.filename
            )));
        }
    }
    Ok(())
}

fn latest_version(migrations: &[EmbeddedMigration]) -> i32 {
    migrations
        .last()
        .map_or(BASELINE_VERSION, |migration| migration.version)
}

fn schema_tables(client: &mut Client, schema: &str) -> Result<BTreeSet<String>, SchemaError> {
    Ok(client
        .query(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1 ORDER BY tablename",
            &[&schema],
        )?
        .into_iter()
        .map(|row| row.get(0))
        .collect())
}

fn classify_baseline_state(
    client: &mut Client,
    schema: &str,
) -> Result<BaselineState, SchemaError> {
    let tables = schema_tables(client, schema)?;
    let has_bookkeeping = tables.contains("schema_migrations");
    if has_bookkeeping && let Some(state) = recognized_baseline_receipt(client, schema)? {
        return Ok(state);
    }
    let application_tables = tables
        .iter()
        .filter(|table| {
            !matches!(
                table.as_str(),
                "gobby_install_ownership" | "schema_migrations"
            )
        })
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    if has_bookkeeping {
        return Ok(BaselineState::CorruptPartial);
    }
    if application_tables.is_empty() {
        return Ok(if tables.contains("gobby_install_ownership") {
            BaselineState::FreshWithInstallInfra
        } else {
            BaselineState::Fresh
        });
    }
    if GCORE_CODE_INDEX_CORE_TABLES
        .iter()
        .all(|table| application_tables.contains(table))
    {
        return Ok(BaselineState::GcoreCodeIndex);
    }
    if GWIKI_TABLES
        .iter()
        .all(|table| application_tables.contains(table))
        && application_tables
            .iter()
            .all(|table| table.starts_with("gwiki_"))
    {
        return Ok(BaselineState::GwikiStandalone);
    }
    Ok(BaselineState::CorruptPartial)
}

fn prior_baseline_receipt(checksum: &str) -> bool {
    PRIOR_RECEIPT_CHECKSUMS
        .iter()
        .any(|(version, prior)| *version == BASELINE_VERSION && *prior == checksum)
}

fn recognized_baseline_receipt(
    client: &mut Client,
    schema: &str,
) -> Result<Option<BaselineState>, SchemaError> {
    let table = qualified_name(schema, "schema_migrations")?;
    let row = client.query_opt(
        &format!("SELECT filename, checksum FROM {table} WHERE version = $1"),
        &[&BASELINE_VERSION],
    )?;
    let Some(row) = row else {
        return Ok(None);
    };
    if row.get::<_, Option<String>>(0).as_deref() != Some("baseline@375") {
        return Ok(None);
    }
    let checksum = row.get::<_, Option<String>>(1);
    Ok(match checksum.as_deref() {
        Some(BASELINE_CHECKSUM) => Some(BaselineState::AlreadyBaselined),
        Some(prior) if prior_baseline_receipt(prior) => Some(BaselineState::AlreadyBaselined),
        Some(TOOL_CHAT_OVERLAY_PREDECESSOR_CHECKSUM) => Some(BaselineState::ParentBaseline),
        Some(WORKTREE_PRE_OVERLAY_BASELINE_CHECKSUM) => Some(BaselineState::PredecessorBaseline),
        Some(PREDECESSOR_BASELINE_CHECKSUM) => Some(BaselineState::PredecessorBaseline),
        Some(PARENT_BASELINE_CHECKSUM) => Some(BaselineState::ParentBaseline),
        Some(WORKTREE_BASELINE_CHECKSUM) => Some(BaselineState::WorktreeBaseline),
        Some(ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM) => {
            Some(BaselineState::AccountIdentityPredecessor)
        }
        _ => None,
    })
}

fn read_schema_head(client: &mut Client, schema: &str) -> Result<i32, SchemaError> {
    if !schema_tables(client, schema)?.contains("schema_migrations") {
        return Ok(0);
    }
    let table = qualified_name(schema, "schema_migrations")?;
    Ok(client
        .query_one(
            &format!("SELECT COALESCE(MAX(version), 0) FROM {table}"),
            &[],
        )?
        .get(0))
}

fn read_source_identity(client: &mut Client) -> Result<SourceIdentity, SchemaError> {
    let row = client.query_one(
        "SELECT (pg_control_system()).system_identifier::text, current_database(), oid \
         FROM pg_database WHERE datname = current_database()",
        &[],
    )?;
    Ok(SourceIdentity {
        pg_system_identifier: row.get(0),
        database_name: row.get(1),
        database_oid: row.get(2),
    })
}

fn require_pg_search(client: &mut Client) -> Result<(), SchemaError> {
    if client
        .query_opt(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_search'",
            &[],
        )?
        .is_none()
    {
        return Err(SchemaError::Unsupported(
            "pg_search extension is required; rebuild the PostgreSQL service image".to_owned(),
        ));
    }
    Ok(())
}

fn apply_baseline(
    client: &mut Client,
    schema: &str,
    state: BaselineState,
) -> Result<(), SchemaError> {
    let rendered = render_baseline_for_schema(schema);
    let statements = split_sql_statements(&rendered)?;
    let receipt_table = qualified_name(schema, "schema_migrations")?;
    let mut transaction = client.transaction()?;
    for statement in statements {
        if let Some(statement) = baseline_statement_for_state(&statement, state) {
            transaction.batch_execute(&statement)?;
        }
    }
    let filename = format!("baseline@{BASELINE_VERSION}");
    if state.updates_baseline_receipt() {
        transaction.execute(
            &format!(
                "UPDATE {receipt_table} SET filename = $1, checksum = $2, applied_at = NOW() \
                 WHERE version = $3"
            ),
            &[&filename, &BASELINE_CHECKSUM, &BASELINE_VERSION],
        )?;
    } else {
        transaction.execute(
            &format!(
                "INSERT INTO {receipt_table}(version, filename, checksum, applied_at) \
                 VALUES ($1, $2, $3, NOW())"
            ),
            &[&BASELINE_VERSION, &filename, &BASELINE_CHECKSUM],
        )?;
    }
    transaction.commit()?;
    Ok(())
}

fn render_baseline_for_schema(schema: &str) -> Cow<'static, str> {
    render_sql_for_schema(BASELINE_SQL, schema)
}

pub(super) fn render_sql_for_schema<'a>(sql: &'a str, schema: &str) -> Cow<'a, str> {
    if schema == "public" {
        return Cow::Borrowed(sql);
    }
    Cow::Owned(
        sql.replace("SCHEMA public", &format!("SCHEMA {schema}"))
            .replace("public.", &format!("{schema}.")),
    )
}

fn baseline_statement_for_state(statement: &str, state: BaselineState) -> Option<String> {
    if let Some(mode) = state.refresh_mode() {
        return baseline_refresh_statement_for_mode(statement, mode).then(|| statement.to_owned());
    }
    if !matches!(
        state,
        BaselineState::GcoreCodeIndex | BaselineState::GwikiStandalone
    ) {
        return Some(statement.to_owned());
    }
    let body = statement_body(statement);
    if state == BaselineState::GcoreCodeIndex && gcore_hop_owns_code_inheritance_ddl(body) {
        return None;
    }
    if let Some(table) = create_table_name(body)
        && (table.starts_with("gwiki_")
            || (state == BaselineState::GcoreCodeIndex
                && GCORE_CODE_INDEX_CORE_TABLES.contains(&table)))
    {
        return None;
    }
    if let Some(table) = create_index_table(body)
        && (table.starts_with("gwiki_")
            || (state == BaselineState::GcoreCodeIndex
                && GCORE_CODE_INDEX_CORE_TABLES.contains(&table)))
    {
        return Some(add_index_if_not_exists(statement));
    }
    if state == BaselineState::GcoreCodeIndex
        && let Some(table) = alter_table_name(body)
        && GCORE_CODE_INDEX_CORE_TABLES.contains(&table)
    {
        let normalized = body.split_whitespace().collect::<Vec<_>>().join(" ");
        if normalized.contains(" ADD GENERATED ALWAYS AS IDENTITY ")
            || normalized.contains(" ADD CONSTRAINT ")
        {
            return None;
        }
    }
    Some(statement.to_owned())
}

fn gcore_hop_owns_code_inheritance_ddl(body: &str) -> bool {
    if create_table_name(body) == Some("code_inheritance")
        || create_index_table(body) == Some("code_inheritance")
        || alter_table_name(body) == Some("code_inheritance")
    {
        return true;
    }
    let normalized = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let upper = normalized.to_ascii_uppercase();
    // FOREACH keeps baseline RLS DO blocks from being classified as code_inheritance DDL.
    (upper.starts_with("GRANT ") || upper.contains(" POLICY "))
        && statement_targets_relation(&normalized, "code_inheritance")
        && !upper.contains("FOREACH")
}

fn statement_targets_relation(statement: &str, relation: &str) -> bool {
    let tokens: Vec<&str> = statement.split_whitespace().collect();
    let Some(on) = tokens
        .iter()
        .position(|token| token.eq_ignore_ascii_case("ON"))
    else {
        return false;
    };
    let Some(first) = tokens.get(on + 1).copied() else {
        return false;
    };
    let raw = if first.eq_ignore_ascii_case("TABLE") || first.eq_ignore_ascii_case("SEQUENCE") {
        tokens.get(on + 2).copied().unwrap_or("")
    } else {
        first
    };
    let name = trim_identifier(raw);
    let name = name
        .rsplit_once('.')
        .map_or(name, |(_, relation_name)| relation_name);
    name == relation
        || name
            .strip_prefix(relation)
            .is_some_and(|suffix| suffix.starts_with('_'))
}

fn statement_body(mut statement: &str) -> &str {
    loop {
        statement = statement.trim_start();
        if let Some(comment) = statement.strip_prefix("--") {
            statement = comment
                .find('\n')
                .map_or("", |newline| &comment[newline + 1..]);
        } else if let Some(comment) = statement.strip_prefix("/*") {
            statement = comment.find("*/").map_or("", |end| &comment[end + 2..]);
        } else {
            return statement;
        }
    }
}

fn create_table_name(statement: &str) -> Option<&str> {
    let tokens = statement.split_whitespace().collect::<Vec<_>>();
    if tokens.len() < 3
        || !tokens[0].eq_ignore_ascii_case("CREATE")
        || !tokens[1].eq_ignore_ascii_case("TABLE")
    {
        return None;
    }
    let index = if tokens
        .get(2)
        .is_some_and(|token| token.eq_ignore_ascii_case("IF"))
    {
        5
    } else {
        2
    };
    tokens.get(index).map(|token| trim_identifier(token))
}

fn alter_table_name(statement: &str) -> Option<&str> {
    let tokens = statement.split_whitespace().collect::<Vec<_>>();
    if tokens.len() < 3
        || !tokens[0].eq_ignore_ascii_case("ALTER")
        || !tokens[1].eq_ignore_ascii_case("TABLE")
    {
        return None;
    }
    let index = if tokens
        .get(2)
        .is_some_and(|token| token.eq_ignore_ascii_case("ONLY"))
    {
        3
    } else {
        2
    };
    tokens.get(index).map(|token| trim_identifier(token))
}

fn create_index_table(statement: &str) -> Option<&str> {
    let tokens = statement.split_whitespace().collect::<Vec<_>>();
    let on = tokens
        .iter()
        .position(|token| token.eq_ignore_ascii_case("ON"))?;
    if !tokens.first()?.eq_ignore_ascii_case("CREATE")
        || !tokens[..on]
            .iter()
            .any(|token| token.eq_ignore_ascii_case("INDEX"))
    {
        return None;
    }
    tokens.get(on + 1).map(|token| {
        trim_identifier(token)
            .rsplit_once('.')
            .map_or(trim_identifier(token), |(_, relation)| relation)
    })
}

fn trim_identifier(value: &str) -> &str {
    value.trim_matches(|character| matches!(character, '"' | ';' | '(' | ')'))
}

fn add_index_if_not_exists(statement: &str) -> String {
    let upper = statement.to_ascii_uppercase();
    if upper.contains("INDEX IF NOT EXISTS") {
        return statement.to_owned();
    }
    let Some(index) = upper.find("INDEX ") else {
        return statement.to_owned();
    };
    let insert_at = index + "INDEX ".len();
    format!(
        "{}IF NOT EXISTS {}",
        &statement[..insert_at],
        &statement[insert_at..]
    )
}

fn verify_adopted_columns(
    client: &mut Client,
    schema: &str,
    state: BaselineState,
) -> Result<(), SchemaError> {
    let contracts = adopted_column_contracts();
    let required = match state {
        BaselineState::GcoreCodeIndex => GCORE_CODE_INDEX_TABLES.as_slice(),
        BaselineState::GwikiStandalone => GWIKI_TABLES.as_slice(),
        _ => return Ok(()),
    };
    for table in required {
        let actual = client
            .query(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
                &[&schema, table],
            )?
            .into_iter()
            .map(|row| row.get::<_, String>(0))
            .collect::<BTreeSet<_>>();
        if actual.is_empty() && *table == "code_inheritance" {
            continue;
        }
        let missing = contracts[*table]
            .iter()
            .filter(|column| !actual.contains(**column))
            .copied()
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            return Err(SchemaError::Unsupported(format!(
                "cannot adopt {table}; missing required columns: {}",
                missing.join(", ")
            )));
        }
    }
    Ok(())
}

fn apply_pending_migrations(
    client: &mut Client,
    schema: &str,
    migrations: &[EmbeddedMigration],
    lineage: BaselineState,
    destructive_authorized: bool,
) -> Result<usize, SchemaError> {
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
        let expected = if version == BASELINE_VERSION {
            Some((format!("baseline@{BASELINE_VERSION}"), BASELINE_CHECKSUM))
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

    let mut count = 0;
    for migration in pending {
        let destructive = has_directive(migration.sql, "-- gobby:destructive");
        let non_transactional = has_directive(migration.sql, "-- gobby:non-transactional");
        if destructive && stamp_destructive {
            stamp_receipt_only(client, &table, migration)?;
        } else if non_transactional {
            apply_non_transactional(client, &table, migration)?;
        } else {
            apply_transactional(client, &table, migration)?;
        }
        count += 1;
    }
    Ok(count)
}

fn stamp_receipt_only(
    client: &mut Client,
    receipt_table: &str,
    migration: &EmbeddedMigration,
) -> Result<(), SchemaError> {
    let mut transaction = client.transaction()?;
    insert_receipt(&mut transaction, receipt_table, migration)?;
    transaction.commit()?;
    Ok(())
}

fn apply_transactional(
    client: &mut Client,
    receipt_table: &str,
    migration: &EmbeddedMigration,
) -> Result<(), SchemaError> {
    let mut transaction = client.transaction()?;
    execute_sql_script(&mut transaction, migration.sql)?;
    insert_receipt(&mut transaction, receipt_table, migration)?;
    transaction.commit()?;
    Ok(())
}

fn apply_non_transactional(
    client: &mut Client,
    receipt_table: &str,
    migration: &EmbeddedMigration,
) -> Result<(), SchemaError> {
    repair_invalid_concurrent_indexes(client, migration.sql)?;
    execute_sql_script(client, migration.sql)?;
    insert_receipt(client, receipt_table, migration)?;
    Ok(())
}

fn execute_sql_script(client: &mut impl GenericClient, sql: &str) -> Result<(), SchemaError> {
    for statement in split_sql_statements(sql)? {
        client.batch_execute(&statement)?;
    }
    Ok(())
}

fn insert_receipt(
    client: &mut impl GenericClient,
    receipt_table: &str,
    migration: &EmbeddedMigration,
) -> Result<(), SchemaError> {
    client.execute(
        &format!(
            "INSERT INTO {receipt_table}(version, filename, checksum, applied_at) VALUES ($1, $2, $3, NOW())"
        ),
        &[&migration.version, &migration.filename, &migration.checksum],
    )?;
    Ok(())
}

fn has_directive(sql: &str, directive: &str) -> bool {
    sql.lines()
        .filter(|line| line.trim_start().starts_with("-- gobby:"))
        .any(|line| line.trim() == directive)
}

fn repair_invalid_concurrent_indexes(client: &mut Client, sql: &str) -> Result<(), SchemaError> {
    for index_name in concurrent_index_names(sql)? {
        let row = client.query_opt(
            r#"
            SELECT format('%I.%I', namespace.nspname, relation.relname)
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_index AS index_state ON index_state.indexrelid = relation.oid
            WHERE relation.oid = to_regclass($1) AND NOT index_state.indisvalid
            "#,
            &[&index_name],
        )?;
        if let Some(row) = row {
            let qualified: String = row.get(0);
            client.batch_execute(&format!("DROP INDEX CONCURRENTLY IF EXISTS {qualified}"))?;
        }
    }
    Ok(())
}

fn concurrent_index_names(sql: &str) -> Result<Vec<String>, SchemaError> {
    let mut names = Vec::new();
    for statement in split_sql_statements(sql)? {
        let body = statement_body(&statement);
        let tokens = body.split_whitespace().collect::<Vec<_>>();
        let Some(index_position) = tokens
            .iter()
            .position(|token| token.eq_ignore_ascii_case("INDEX"))
        else {
            continue;
        };
        if !tokens
            .first()
            .is_some_and(|token| token.eq_ignore_ascii_case("CREATE"))
            || !tokens
                .get(index_position + 1)
                .is_some_and(|token| token.eq_ignore_ascii_case("CONCURRENTLY"))
        {
            continue;
        }
        let mut name_position = index_position + 2;
        if tokens
            .get(name_position)
            .is_some_and(|token| token.eq_ignore_ascii_case("IF"))
        {
            name_position += 3;
        }
        let Some(name) = tokens.get(name_position) else {
            return Err(SchemaError::Unsupported(
                "CREATE INDEX CONCURRENTLY is missing an index name".to_owned(),
            ));
        };
        let name = name.trim_matches('"').to_owned();
        if !names.contains(&name) {
            names.push(name);
        }
    }
    Ok(names)
}

fn is_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    matches!(bytes.next(), Some(byte) if byte.is_ascii_alphabetic() || byte == b'_')
        && bytes.all(is_identifier_byte)
}

fn is_identifier_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'$'
}
