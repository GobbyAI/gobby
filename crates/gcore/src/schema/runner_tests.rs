use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::sync::{Mutex, mpsc};
use std::time::Duration as StdDuration;

use postgres::{Client, Config, NoTls, error::SqlState};
use time::format_description::well_known::Rfc3339;
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use super::assets::BASELINE_SQL;
use super::assets::{BASELINE_CHECKSUM, BASELINE_VERSION, EmbeddedMigration, MIGRATIONS};
use super::baseline_refresh::{
    REFRESH_STATEMENT_PREFIXES, REMOVED_STATEMENT_PREFIXES, RUNTIME_BOUNDARY_REFRESH_PREFIXES,
    RefreshMode, TYPED_DOMAIN_REFRESH_PREFIXES, baseline_refresh_statement,
    baseline_refresh_statement_for_mode, baseline_removed_statement,
};
use super::error::SchemaError;
use super::gate::{
    BackupGateContext, SourceIdentity, VerifiedBackupManifest, parse_backup_manifest,
};
use super::runner::{
    ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM, PARENT_BASELINE_CHECKSUM, PREDECESSOR_BASELINE_CHECKSUM,
    SchemaRunner, WORKTREE_BASELINE_CHECKSUM,
};
use super::sql_splitter::split_sql_statements;

static RECOVERY_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 376,
    filename: "376_recovery_probe.sql",
    checksum: "d63e14df78da3519a30caf2dac74341ab5f0c9aa05f7bec58174ec0adf383159",
    sql: "-- gobby:non-transactional\nCREATE UNIQUE INDEX CONCURRENTLY schema_recovery_idx ON recovery_values(id);\n",
};
static RECOVERY_MIGRATIONS: &[EmbeddedMigration] = &[RECOVERY_MIGRATION];

static DESTRUCTIVE_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 376,
    filename: "376_destructive_probe.sql",
    checksum: "c10820fc8be4c2bceab1610fd8372c8d864fd7c4a8985773cf903bae450b19e9",
    sql: "-- gobby:destructive\nCREATE TABLE gate_probe (id integer);\n",
};
static DESTRUCTIVE_MIGRATIONS: &[EmbeddedMigration] = &[DESTRUCTIVE_MIGRATION];

static GUARDED_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 376,
    filename: "376_guarded_probe.sql",
    checksum: "8d86f80f785ac4f918ce34ea7f0dca860266e91dbba95d8a2be0965a9cdd147a",
    sql: "DO $guard$\nBEGIN\n  IF to_regclass('legacy_probe_source') IS NOT NULL THEN\n    CREATE TABLE IF NOT EXISTS guarded_probe_copied (id integer);\n  END IF;\nEND\n$guard$;\n",
};
static GUARDED_MIGRATIONS: &[EmbeddedMigration] = &[GUARDED_MIGRATION];

static COPY_THEN_FENCE: EmbeddedMigration = EmbeddedMigration {
    version: 376,
    filename: "376_copy_probe.sql",
    checksum: "7ec5f3b7cf557fcee6903676bc89a7ff89ed0c1100e44775e5df1a01d3c38689",
    sql: "CREATE TABLE copy_probe (id integer);\n",
};
static DESTRUCTIVE_AFTER_COPY: EmbeddedMigration = EmbeddedMigration {
    version: 377,
    filename: "377_destructive_probe.sql",
    checksum: "c5824af6e3aa4151609e330dca97948d7ba3a22293248883d5fc4d335165638e",
    sql: "-- gobby:destructive\nCREATE TABLE drop_probe (id integer);\n",
};
static COPY_THEN_DESTRUCTIVE: &[EmbeddedMigration] = &[COPY_THEN_FENCE, DESTRUCTIVE_AFTER_COPY];

static DATABASE_TEST_LOCK: Mutex<()> = Mutex::new(());

const GCODE_RLS_TABLES: [&str; 10] = [
    "code_indexed_projects",
    "code_indexed_project_states",
    "code_indexed_file_states",
    "code_indexed_files",
    "code_symbols",
    "code_imports",
    "code_calls",
    "code_content_chunks",
    "code_index_projection_cleanup_pending",
    "code_index_prune_dirty_projects",
];

struct ScratchDatabase {
    admin: Client,
    config: Config,
    name: String,
}

impl ScratchDatabase {
    fn create(database_url: &str) -> anyhow::Result<(Self, Client)> {
        let config: Config = database_url.parse()?;
        let mut admin = config.connect(NoTls)?;
        let name = format!("gcore_schema_unit_{}", Uuid::new_v4().simple());
        admin.batch_execute(&format!("CREATE DATABASE {name}"))?;
        let mut scratch_config = config;
        scratch_config.dbname(&name);
        let mut client = scratch_config.connect(NoTls)?;
        client.batch_execute("CREATE EXTENSION IF NOT EXISTS pg_search")?;
        Ok((
            Self {
                admin,
                config: scratch_config,
                name,
            },
            client,
        ))
    }

    fn connect(&self) -> Result<Client, postgres::Error> {
        self.config.connect(NoTls)
    }

    fn connect_as(&self, user: &str, password: &str) -> Result<Client, postgres::Error> {
        let mut config = self.config.clone();
        config.user(user).password(password);
        config.connect(NoTls)
    }
}

impl Drop for ScratchDatabase {
    fn drop(&mut self) {
        let _ = self.admin.batch_execute(&format!(
            "DROP DATABASE IF EXISTS {} WITH (FORCE)",
            self.name
        ));
    }
}

struct ScratchPath(std::path::PathBuf);

impl Drop for ScratchPath {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn test_database() -> anyhow::Result<Option<(ScratchDatabase, Client)>> {
    let Ok(database_url) = env::var("GOBBY_SCHEMA_TEST_DATABASE_URL") else {
        eprintln!("GOBBY_SCHEMA_TEST_DATABASE_URL is unset; skipping PostgreSQL schema test");
        return Ok(None);
    };
    ScratchDatabase::create(&database_url).map(Some)
}

fn install_baseline(client: &mut Client) -> anyhow::Result<()> {
    SchemaRunner::new(client, "public")?.apply()?;
    Ok(())
}

fn install_predecessor(client: &mut Client) -> anyhow::Result<()> {
    install_lineage_fixture(
        client,
        include_str!("../../tests/fixtures/schema/predecessor_baseline.sql"),
        PREDECESSOR_BASELINE_CHECKSUM,
    )
}

fn install_parent_baseline(client: &mut Client) -> anyhow::Result<()> {
    install_lineage_fixture(
        client,
        include_str!("../../tests/fixtures/schema/parent_baseline.sql"),
        PARENT_BASELINE_CHECKSUM,
    )
}

fn install_worktree_baseline(client: &mut Client) -> anyhow::Result<()> {
    install_lineage_fixture(
        client,
        include_str!("../../tests/fixtures/schema/worktree_baseline.sql"),
        WORKTREE_BASELINE_CHECKSUM,
    )
}

fn install_lineage_fixture(client: &mut Client, sql: &str, checksum: &str) -> anyhow::Result<()> {
    for statement in split_sql_statements(sql)? {
        client.batch_execute(&statement)?;
    }
    client.execute(
        "INSERT INTO schema_migrations(version, filename, checksum, applied_at) \
         VALUES ($1, $2, $3, NOW())",
        &[
            &BASELINE_VERSION,
            &format!("baseline@{BASELINE_VERSION}"),
            &checksum,
        ],
    )?;
    Ok(())
}

fn projection_change_sequence_cache_size(client: &mut Client) -> anyhow::Result<i64> {
    Ok(client
        .query_one(
            "SELECT seqcache FROM pg_sequence \
             WHERE seqrelid = pg_get_serial_sequence(\
                 'embedding_projection_changes', 'sequence'\
             )::regclass",
            &[],
        )?
        .get(0))
}

fn assert_runtime_crud_privileges(client: &mut Client, table: &str) -> anyhow::Result<()> {
    for privilege in ["SELECT", "INSERT", "UPDATE", "DELETE"] {
        let granted: bool = client
            .query_one(
                "SELECT has_table_privilege('gobby_daemon_runtime', $1, $2)",
                &[&table, &privilege],
            )?
            .get(0);
        assert!(
            granted,
            "runtime role lacks {privilege} privilege on {table}"
        );
    }
    Ok(())
}

fn assert_gcode_rls_policies(client: &mut Client) -> anyhow::Result<()> {
    let rows = client.query(
        "SELECT tablename, policyname, cmd, COALESCE(qual, ''), COALESCE(with_check, '') \
         FROM pg_policies \
         WHERE schemaname = 'public' \
         AND roles @> ARRAY['gobby_gcode_capability']::name[] \
         AND policyname LIKE 'gobby_gcode_project_%' \
         ORDER BY tablename, policyname",
        &[],
    )?;
    let mut actual = BTreeMap::<String, Vec<(String, String, String, String)>>::new();
    for row in rows {
        actual.entry(row.get(0)).or_default().push((
            row.get(1),
            row.get(2),
            row.get(3),
            row.get(4),
        ));
    }
    let expected = [
        ("gobby_gcode_project_delete".to_owned(), "DELETE".to_owned()),
        ("gobby_gcode_project_insert".to_owned(), "INSERT".to_owned()),
        ("gobby_gcode_project_read".to_owned(), "SELECT".to_owned()),
        ("gobby_gcode_project_update".to_owned(), "UPDATE".to_owned()),
    ];
    for table in GCODE_RLS_TABLES {
        let policies = actual
            .remove(table)
            .expect("gcode table must have RLS policies");
        let operations: Vec<(String, String)> = policies
            .iter()
            .map(|(name, command, _, _)| (name.clone(), command.clone()))
            .collect();
        assert_eq!(operations, expected);
        for (_, _, using_expression, check_expression) in policies {
            let predicate = format!("{using_expression} {check_expression}");
            assert!(predicate.contains("current_code_overlay_project_id"));
            assert!(predicate.contains("current_project_id"));
            if [
                "code_indexed_project_states",
                "code_indexed_file_states",
                "code_index_prune_dirty_projects",
            ]
            .contains(&table)
            {
                assert!(predicate.contains("current_machine_id"));
            }
            assert_ne!(predicate.trim().to_ascii_lowercase(), "true");
        }
    }
    assert!(
        actual.is_empty(),
        "unexpected gcode RLS policies: {actual:?}"
    );
    Ok(())
}

fn source_identity(client: &mut Client) -> anyhow::Result<SourceIdentity> {
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

#[test]
fn fresh_baseline_creates_config_revision_state() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    install_baseline(&mut client)?;

    let revision: i64 = client
        .query_one("SELECT revision FROM config_state WHERE id", &[])?
        .get(0);
    assert_eq!(revision, 0);
    let config_revision: i64 = client
        .query_one(
            "INSERT INTO config_store(key, value) VALUES ('test.key', 'value') RETURNING revision",
            &[],
        )?
        .get(0);
    assert_eq!(config_revision, 0);
    Ok(())
}

#[test]
fn fresh_baseline_creates_embedding_coordination_state() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    install_baseline(&mut client)?;

    let coordination_columns: Vec<String> = client
        .query(
            "SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'embedding_generation_acks'
             ORDER BY ordinal_position",
            &[],
        )?
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(
        coordination_columns,
        [
            "daemon_instance_id",
            "generation",
            "committed_revision",
            "acknowledged",
            "lease_expires_at",
            "updated_at",
        ]
    );
    let change_columns: Vec<String> = client
        .query(
            "SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'embedding_projection_changes'
             ORDER BY ordinal_position",
            &[],
        )?
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(
        change_columns,
        [
            "sequence",
            "source_kind",
            "source_id",
            "is_tombstone",
            "created_at",
        ]
    );
    assert_eq!(projection_change_sequence_cache_size(&mut client)?, 1);
    for table in ["embedding_generation_acks", "embedding_projection_changes"] {
        assert_runtime_crud_privileges(&mut client, table)?;
    }
    Ok(())
}

#[test]
fn obsolete_baseline_receipt_is_rejected_without_mutation() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    client.execute(
        "INSERT INTO config_store(key, value) VALUES ('preserved.key', 'preserved')",
        &[],
    )?;
    client.execute(
        "UPDATE schema_migrations SET checksum = $1 WHERE version = $2",
        &[&ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM, &BASELINE_VERSION],
    )?;

    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("obsolete baseline receipts require a dedicated campaign");

    assert!(
        error
            .to_string()
            .contains("run 'gobby hub-maintenance run account-identity-cutover'")
    );
    let row = client.query_one(
        "SELECT value, revision FROM config_store WHERE key = 'preserved.key'",
        &[],
    )?;
    assert_eq!(row.get::<_, String>(0), "preserved");
    assert_eq!(row.get::<_, i64>(1), 0);
    let checksum: String = client
        .query_one(
            "SELECT checksum FROM schema_migrations WHERE version = $1",
            &[&BASELINE_VERSION],
        )?
        .get(0);
    assert_eq!(checksum, ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM);
    Ok(())
}

#[test]
fn predecessor_checksum_matches_python_cutover_contract() {
    const PYTHON_CUTOVER_SOURCE: &str =
        include_str!("../../../../src/gobby/storage/account_identity_cutover.py");
    let declaration = PYTHON_CUTOVER_SOURCE
        .lines()
        .find(|line| line.starts_with("PREDECESSOR_BASELINE_CHECKSUM = "))
        .expect("Python cutover must declare its predecessor baseline checksum");
    let python_checksum = declaration
        .split_once('=')
        .map(|(_, value)| value.trim().trim_matches('"'))
        .expect("Python predecessor checksum declaration must contain '='");

    assert_eq!(python_checksum, ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM);
}

#[test]
fn predecessor_fixture_matches_pinned_checksum() {
    const PREDECESSOR_BASELINE_SQL: &str =
        include_str!("../../tests/fixtures/schema/predecessor_baseline.sql");
    assert_eq!(
        super::assets::sha256_hex(PREDECESSOR_BASELINE_SQL.as_bytes()),
        PREDECESSOR_BASELINE_CHECKSUM
    );
}

#[test]
fn baseline_refresh_accepts_exactly_the_predecessor_statement_difference() {
    const PREDECESSOR_BASELINE_SQL: &str =
        include_str!("../../tests/fixtures/schema/predecessor_baseline.sql");
    let current = split_sql_statements(BASELINE_SQL).expect("current baseline splits");
    let predecessor =
        split_sql_statements(PREDECESSOR_BASELINE_SQL).expect("predecessor baseline splits");
    let predecessor_set = predecessor.iter().cloned().collect::<BTreeSet<_>>();
    let current_set = current.iter().cloned().collect::<BTreeSet<_>>();
    let added = current
        .iter()
        .filter(|statement| !predecessor_set.contains(*statement))
        .cloned()
        .collect::<Vec<_>>();
    let removed = predecessor
        .iter()
        .filter(|statement| !current_set.contains(*statement))
        .cloned()
        .collect::<Vec<_>>();

    let unexpected = added
        .iter()
        .filter(|statement| !baseline_refresh_statement(statement))
        .cloned()
        .collect::<Vec<_>>();
    assert!(
        unexpected.is_empty(),
        "refresh acceptance missed added statements: {unexpected:?}"
    );

    let matched = REFRESH_STATEMENT_PREFIXES
        .iter()
        .map(|prefix| {
            added
                .iter()
                .filter(|statement| statement.trim_start().starts_with(prefix))
                .count()
        })
        .collect::<Vec<_>>();
    assert!(
        matched.iter().all(|count| *count == 1),
        "each refresh prefix must match exactly one added statement: {matched:?}"
    );

    let unexpected_removed = removed
        .iter()
        .filter(|statement| !baseline_removed_statement(statement))
        .cloned()
        .collect::<Vec<_>>();
    assert!(
        unexpected_removed.is_empty(),
        "refresh removal allowlist missed predecessor statements: {unexpected_removed:?}"
    );

    let removed_matched = REMOVED_STATEMENT_PREFIXES
        .iter()
        .map(|prefix| {
            removed
                .iter()
                .filter(|statement| statement.trim_start().starts_with(prefix))
                .count()
        })
        .collect::<Vec<_>>();
    assert!(
        removed_matched.iter().all(|count| *count <= 1),
        "each removed prefix must match at most one predecessor statement: {removed_matched:?}"
    );
    assert!(
        removed_matched.contains(&1),
        "removed-statement allowlist must name the dropped predecessor DDL"
    );

    let lingering = current
        .iter()
        .filter(|statement| baseline_removed_statement(statement))
        .cloned()
        .collect::<Vec<_>>();
    assert!(
        lingering.is_empty(),
        "current baseline still contains removed legacy statements: {lingering:?}"
    );
}

#[test]
fn combined_refresh_prefixes_are_the_union_of_the_mode_lists() {
    let mut expected = TYPED_DOMAIN_REFRESH_PREFIXES.to_vec();
    expected.extend_from_slice(RUNTIME_BOUNDARY_REFRESH_PREFIXES);
    assert_eq!(REFRESH_STATEMENT_PREFIXES, expected.as_slice());
}

#[test]
fn parent_and_worktree_fixtures_match_pinned_checksums() {
    assert_eq!(
        super::assets::sha256_hex(
            include_str!("../../tests/fixtures/schema/parent_baseline.sql").as_bytes()
        ),
        PARENT_BASELINE_CHECKSUM
    );
    assert_eq!(
        super::assets::sha256_hex(
            include_str!("../../tests/fixtures/schema/worktree_baseline.sql").as_bytes()
        ),
        WORKTREE_BASELINE_CHECKSUM
    );
}

fn mode_covers_added_statements(
    predecessor_sql: &str,
    mode: RefreshMode,
    prefixes: &[&str],
) -> Result<(), String> {
    let current = split_sql_statements(BASELINE_SQL).expect("current baseline splits");
    let predecessor = split_sql_statements(predecessor_sql).expect("lineage baseline splits");
    let predecessor_set = predecessor.iter().cloned().collect::<BTreeSet<_>>();
    let added = current
        .iter()
        .filter(|statement| !predecessor_set.contains(*statement))
        .cloned()
        .collect::<Vec<_>>();
    let unexpected = added
        .iter()
        .filter(|statement| !baseline_refresh_statement_for_mode(statement, mode))
        .cloned()
        .collect::<Vec<_>>();
    if !unexpected.is_empty() {
        return Err(format!(
            "refresh mode {mode:?} missed added statements: {unexpected:?}"
        ));
    }
    let matched = prefixes
        .iter()
        .map(|prefix| {
            added
                .iter()
                .filter(|statement| statement.trim_start().starts_with(prefix))
                .count()
        })
        .collect::<Vec<_>>();
    if !matched.iter().all(|count| *count == 1) {
        return Err(format!(
            "each {mode:?} prefix must match exactly one added statement: {matched:?}"
        ));
    }
    Ok(())
}

#[test]
fn parent_baseline_refresh_is_runtime_boundary_only() {
    let result = mode_covers_added_statements(
        include_str!("../../tests/fixtures/schema/parent_baseline.sql"),
        RefreshMode::RuntimeOnly,
        RUNTIME_BOUNDARY_REFRESH_PREFIXES,
    );
    assert!(result.is_ok(), "{result:?}");
}

#[test]
fn worktree_baseline_refresh_is_typed_domain_only() {
    let result = mode_covers_added_statements(
        include_str!("../../tests/fixtures/schema/worktree_baseline.sql"),
        RefreshMode::TypedDomainOnly,
        TYPED_DOMAIN_REFRESH_PREFIXES,
    );
    assert!(result.is_ok(), "{result:?}");
}

#[test]
fn receipt_chain_advances_from_19645_and_lineage_checksums() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let fresh = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(fresh.baseline_applied);
    let current: String = client
        .query_one(
            "SELECT checksum FROM schema_migrations WHERE version = $1",
            &[&BASELINE_VERSION],
        )?
        .get(0);
    assert_eq!(current, BASELINE_CHECKSUM);

    let already = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(!already.baseline_applied);

    client.execute(
        "UPDATE schema_migrations SET checksum = $1 WHERE version = $2",
        &[&ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM, &BASELINE_VERSION],
    )?;
    let pre_19645 = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("pre-#19645 hubs must name the cutover path");
    assert!(
        pre_19645
            .to_string()
            .contains("run 'gobby hub-maintenance run account-identity-cutover'")
    );
    let after_reject: String = client
        .query_one(
            "SELECT checksum FROM schema_migrations WHERE version = $1",
            &[&BASELINE_VERSION],
        )?
        .get(0);
    assert_eq!(after_reject, ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM);

    client.execute(
        "UPDATE schema_migrations SET checksum = 'unrecognized' WHERE version = $1",
        &[&BASELINE_VERSION],
    )?;
    let mismatch = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("arbitrary receipt checksums must remain corrupt");
    assert!(
        mismatch
            .to_string()
            .contains("recreate from a verified backup")
    );
    Ok(())
}

#[test]
fn parent_only_lineage_refreshes_runtime_objects() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_parent_baseline(&mut client)?;
    let before: bool = client
        .query_one("SELECT to_regclass('deployment_runtime') IS NOT NULL", &[])?
        .get(0);
    assert!(!before);
    let report = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(report.baseline_applied);
    let after: bool = client
        .query_one("SELECT to_regclass('deployment_runtime') IS NOT NULL", &[])?
        .get(0);
    assert!(after);
    let checksum: String = client
        .query_one(
            "SELECT checksum FROM schema_migrations WHERE version = $1",
            &[&BASELINE_VERSION],
        )?
        .get(0);
    assert_eq!(checksum, BASELINE_CHECKSUM);
    Ok(())
}

#[test]
fn worktree_only_lineage_adds_typed_domain_then_copy_migrations() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_worktree_baseline(&mut client)?;
    let agents_before: bool = client
        .query_one("SELECT to_regclass('agent_definitions') IS NOT NULL", &[])?
        .get(0);
    assert!(!agents_before);
    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("worktree lineage must refuse the destructive drop without a backup");
    assert!(error.to_string().contains("verified hub backup"));
    let agents_after_refresh: bool = client
        .query_one("SELECT to_regclass('agent_definitions') IS NOT NULL", &[])?
        .get(0);
    assert!(
        agents_after_refresh,
        "typed-domain refresh must land before the destructive drop is refused"
    );
    Ok(())
}

#[test]
fn isolated_gcode_principal_reads_parent_and_writes_only_its_overlay() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;

    let machine_id = Uuid::new_v4();
    let other_machine_id = Uuid::new_v4();
    let owner_user_id = Uuid::new_v4();
    let project_id = Uuid::new_v4();
    let unrelated_project_id = Uuid::new_v4();
    let parent_session_id = Uuid::new_v4();
    let child_session_id = Uuid::new_v4();
    let agent_run_id = Uuid::new_v4();
    let worktree_id = Uuid::new_v4();
    let execution_id = Uuid::new_v4();
    let overlay_project_id = Uuid::parse_str("bee23f80-d127-5e8f-9dd1-30670378e19a")?;
    let password = format!("gobby-schema-rls-{}", Uuid::new_v4().simple());

    client.execute(
        "INSERT INTO users(id, email, name, password_hash) \
         VALUES ($1, 'schema-test@example.invalid', 'Schema Test', 'test-only')",
        &[&owner_user_id],
    )?;
    client.execute(
        "INSERT INTO machines(id, hostname, owner_user_id) \
         VALUES ($1, 'schema-test', $3), ($2, 'other', $3)",
        &[&machine_id, &other_machine_id, &owner_user_id],
    )?;
    client.execute(
        "INSERT INTO projects(id, name, repo_path) \
         VALUES ($1, 'parent', '/tmp/gobby-parent'), ($2, 'unrelated', '/tmp/unrelated')",
        &[&project_id, &unrelated_project_id],
    )?;
    client.execute(
        "INSERT INTO sessions(id, external_id, machine_id, source, project_id) \
         VALUES ($1, 'schema-parent', $2, 'test', $3)",
        &[&parent_session_id, &machine_id, &project_id],
    )?;
    client.execute(
        "INSERT INTO worktrees(id, project_id, machine_id, branch_name, worktree_path) \
         VALUES ($1, $2, $3, 'schema-test', '/tmp/gobby-rls-overlay')",
        &[&worktree_id, &project_id, &machine_id],
    )?;
    client.execute(
        "INSERT INTO agent_runs(id, machine_id, parent_session_id, provider, prompt, worktree_id) \
         VALUES ($1, $2, $3, 'test', 'schema test', $4)",
        &[&agent_run_id, &machine_id, &parent_session_id, &worktree_id],
    )?;
    client.execute(
        "INSERT INTO sessions( \
             id, external_id, machine_id, source, project_id, parent_session_id, agent_run_id \
         ) VALUES ($1, 'schema-child', $2, 'test', $3, $4, $5)",
        &[
            &child_session_id,
            &machine_id,
            &project_id,
            &parent_session_id,
            &agent_run_id,
        ],
    )?;

    let role_name: String = client
        .query_one(
            "SELECT role_name::TEXT FROM gobby_agent_auth.issue_principal( \
                 $1, 'agent_run', $2, $3, $4, \
                 clock_timestamp() + INTERVAL '10 minutes', $5 \
             )",
            &[
                &execution_id,
                &child_session_id,
                &agent_run_id,
                &machine_id,
                &password,
            ],
        )?
        .get(0);
    let bound_overlay_id: Option<Uuid> = client
        .query_one(
            "SELECT code_overlay_project_id FROM gobby_agent_auth.principal_bindings \
             WHERE managed_execution_id = $1",
            &[&execution_id],
        )?
        .get(0);
    assert_eq!(bound_overlay_id, Some(overlay_project_id));

    client.execute(
        "INSERT INTO code_indexed_projects(id) VALUES ($1), ($2)",
        &[&project_id, &unrelated_project_id],
    )?;
    client.execute(
        "INSERT INTO code_indexed_project_states(machine_id, project_id, root_path) \
         VALUES ($1, $2, '/tmp/gobby-parent'), ($3, $2, '/tmp/gobby-parent')",
        &[&machine_id, &project_id, &other_machine_id],
    )?;
    client.execute(
        "INSERT INTO code_indexed_files( \
             id, project_id, file_path, language, content_hash \
         ) VALUES ($1, $2, 'parent.rs', 'rust', 'parent'), \
                  ($3, $2, 'other-machine.rs', 'rust', 'other')",
        &[&Uuid::new_v4(), &project_id, &Uuid::new_v4()],
    )?;
    client.execute(
        "INSERT INTO code_indexed_file_states(machine_id, project_id, file_path, content_hash) \
         VALUES ($1, $2, 'parent.rs', 'parent'), \
                ($3, $2, 'other-machine.rs', 'other')",
        &[&machine_id, &project_id, &other_machine_id],
    )?;

    let validation = match database.connect_as(&role_name, &password) {
        Ok(mut scoped) => (|| -> anyhow::Result<()> {
            let revision: i64 = scoped
                .query_one("SELECT revision FROM config_state WHERE id = true", &[])?
                .get(0);
            assert_eq!(
                revision, 0,
                "scoped gcode principal must read config revision"
            );
            let parent_exists: bool = scoped
                .query_one(
                    "SELECT EXISTS( \
                         SELECT 1 FROM code_indexed_file_states \
                         WHERE machine_id = $1 AND project_id = $2 \
                     )",
                    &[&machine_id, &project_id],
                )?
                .get(0);
            assert!(parent_exists, "spawn preflight must see the parent index");
            let visible_parent_states: i64 = scoped
                .query_one(
                    "SELECT count(*) FROM code_indexed_file_states WHERE project_id = $1",
                    &[&project_id],
                )?
                .get(0);
            assert_eq!(
                visible_parent_states, 1,
                "machine scope must filter parent state"
            );

            scoped.execute(
                "INSERT INTO code_indexed_projects(id) VALUES ($1)",
                &[&overlay_project_id],
            )?;
            scoped.execute(
                "INSERT INTO code_indexed_project_states( \
                     machine_id, project_id, root_path, total_files, total_symbols \
                 ) VALUES ($1, $2, '/tmp/gobby-rls-overlay', 1, 0)",
                &[&machine_id, &overlay_project_id],
            )?;
            let visible_projects: i64 = scoped
                .query_one("SELECT count(*) FROM code_indexed_projects", &[])?
                .get(0);
            assert_eq!(
                visible_projects, 2,
                "only parent and overlay must be readable"
            );

            assert_eq!(
                scoped.execute(
                    "UPDATE code_indexed_projects SET updated_at = NOW() WHERE id = $1",
                    &[&project_id],
                )?,
                0,
                "isolated principal must not update parent facts",
            );
            assert_eq!(
                scoped.execute(
                    "DELETE FROM code_indexed_projects WHERE id = $1",
                    &[&project_id]
                )?,
                0,
                "isolated principal must not delete parent facts",
            );
            let error = scoped
                .execute(
                    "UPDATE code_indexed_project_states SET machine_id = $1 \
                     WHERE machine_id = $2 AND project_id = $3",
                    &[&other_machine_id, &machine_id, &overlay_project_id],
                )
                .expect_err("overlay writes for another machine must fail RLS");
            assert_eq!(error.code(), Some(&SqlState::INSUFFICIENT_PRIVILEGE));
            Ok(())
        })(),
        Err(error) => Err(error.into()),
    };

    let revoke_result = client.query_one(
        "SELECT gobby_agent_auth.revoke_principal($1, 1)",
        &[&execution_id],
    );
    validation?;
    revoke_result?;
    assert_gcode_rls_policies(&mut client)?;
    Ok(())
}

#[test]
fn config_revision_baseline_is_nondestructive() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    client.execute(
        "INSERT INTO config_store(key, value) VALUES ('stable.key', 'stable')",
        &[],
    )?;

    let first = SchemaRunner::new(&mut client, "public")?.apply()?;
    let second = SchemaRunner::new(&mut client, "public")?.apply()?;

    assert!(!first.baseline_applied);
    assert!(!second.baseline_applied);
    let value: String = client
        .query_one(
            "SELECT value FROM config_store WHERE key = 'stable.key'",
            &[],
        )?
        .get(0);
    assert_eq!(value, "stable");
    Ok(())
}

#[test]
fn unrecognized_receipt_still_rejects() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;
    client.execute(
        "UPDATE schema_migrations SET checksum = 'unrecognized' WHERE version = $1",
        &[&BASELINE_VERSION],
    )?;

    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("arbitrary receipt checksums must remain corrupt");
    assert!(
        error
            .to_string()
            .contains("recreate from a verified backup")
    );

    client.execute(
        "UPDATE schema_migrations SET filename = 'unexpected@375', checksum = $1 WHERE version = $2",
        &[&BASELINE_CHECKSUM, &BASELINE_VERSION],
    )?;
    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("arbitrary receipt filenames must remain corrupt");
    assert!(
        error
            .to_string()
            .contains("recreate from a verified backup")
    );
    Ok(())
}

#[test]
fn lock_and_recovery_tests_repair_an_invalid_concurrent_index() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::with_migrations_for_test(&mut client, "public", &[])?.apply()?;
    client.batch_execute(
        "CREATE TABLE recovery_values(id integer); INSERT INTO recovery_values VALUES (1), (1)",
    )?;
    let first_error =
        SchemaRunner::with_migrations_for_test(&mut client, "public", RECOVERY_MIGRATIONS)?
            .apply()
            .expect_err("duplicate values must interrupt concurrent unique-index creation");
    assert!(matches!(first_error, SchemaError::Postgres(_)));
    let invalid: bool = client
        .query_one(
            "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = 'schema_recovery_idx'::regclass",
            &[],
        )?
        .get(0);
    assert!(invalid);

    client.execute(
        "DELETE FROM recovery_values WHERE ctid IN (SELECT ctid FROM recovery_values LIMIT 1)",
        &[],
    )?;
    let report =
        SchemaRunner::with_migrations_for_test(&mut client, "public", RECOVERY_MIGRATIONS)?
            .apply()?;
    assert_eq!(report.migrations_applied, 1);
    let valid: bool = client
        .query_one(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = 'schema_recovery_idx'::regclass",
            &[],
        )?
        .get(0);
    assert!(valid);
    Ok(())
}

#[test]
fn lock_and_recovery_tests_named_schema_locks_are_independent() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut lock_client)) = test_database()? else {
        return Ok(());
    };
    let mut apply_client = database.connect()?;
    for schema in ["schema_lock_a", "schema_lock_b"] {
        SchemaRunner::new(&mut lock_client, schema)?.apply()?;
    }
    lock_client.batch_execute("SET search_path TO schema_lock_a, pg_catalog")?;
    lock_client.query_one(
        "SELECT pg_advisory_lock(hashtext('postgres_migrations_apply'), hashtext(current_schema()))",
        &[],
    )?;

    let (sender, receiver) = mpsc::channel();
    let worker = std::thread::spawn(move || {
        let result = SchemaRunner::new(&mut apply_client, "schema_lock_b")
            .and_then(|mut runner| runner.apply())
            .map_err(|error| error.to_string());
        sender.send(result).expect("receiver remains available");
    });
    let result = receiver
        .recv_timeout(StdDuration::from_secs(3))
        .expect("different schema lock must remain available");
    lock_client.query_one(
        "SELECT pg_advisory_unlock(hashtext('postgres_migrations_apply'), hashtext(current_schema()))",
        &[],
    )?;
    worker.join().expect("schema worker must not panic");
    result.map_err(anyhow::Error::msg)?;
    Ok(())
}

#[test]
fn gate_tests_destructive_apply_requires_a_verified_v2_backup() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::with_migrations_for_test(&mut client, "public", &[])?.apply()?;
    let error =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply()
            .expect_err("default apply must halt at a destructive migration");
    assert!(error.to_string().contains("verified hub backup"));

    let fixture = include_str!("../../tests/fixtures/hub_backup_manifest/v2_roundtrip.json");
    let mut manifest = parse_backup_manifest(fixture)?;
    let database_head: i32 = client
        .query_one(
            "SELECT COALESCE(MAX(version), 0)::int FROM schema_migrations",
            &[],
        )?
        .get(0);
    manifest.backup_starting_head = database_head;
    let root = env::temp_dir().join(format!("gcore-backup-gate-{}", Uuid::new_v4()));
    let _scratch_path = ScratchPath(root.clone());
    fs::create_dir_all(root.join("postgres"))?;
    fs::write(root.join("postgres/fixture.dump"), [])?;
    manifest.artifacts[0].sha256 =
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_owned();
    let fixture_identity = manifest.source_identity.clone();
    let created_at = OffsetDateTime::parse(&manifest.created_at, &Rfc3339)?;
    let mut context = BackupGateContext::new(
        &root,
        &fixture_identity,
        database_head,
        created_at + Duration::hours(1),
    );
    context.max_age = Duration::hours(2);
    let verified = VerifiedBackupManifest::verify(manifest.clone(), &context)?;

    let error =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply_with_backup(&verified)
            .expect_err("runner must reject a backup verified for another database");
    assert!(error.to_string().contains("source identity"));

    let identity = source_identity(&mut client)?;
    manifest.source_identity = identity.clone();
    let mut context = BackupGateContext::new(
        &root,
        &identity,
        database_head,
        created_at + Duration::hours(1),
    );
    context.max_age = Duration::hours(2);
    let verified = VerifiedBackupManifest::verify(manifest, &context)?;

    let report =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply_with_backup(&verified)?;
    assert_eq!(report.migrations_applied, 1);
    let table_exists: bool = client
        .query_one("SELECT to_regclass('gate_probe') IS NOT NULL", &[])?
        .get(0);
    assert!(table_exists);
    Ok(())
}

#[test]
fn migrations_directory_exists_and_copy_agent_entry_is_registered() {
    let migrations_dir =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("assets/schema/migrations");
    assert!(
        migrations_dir.is_dir(),
        "crates/gcore/assets/schema/migrations must exist so later leaves can register include_str entries"
    );
    assert_eq!(MIGRATIONS.len(), 11);
    assert_eq!(MIGRATIONS[0].version, 376);
    assert_eq!(MIGRATIONS[0].filename, "376_copy_agent_definitions.sql");
    assert_eq!(MIGRATIONS[1].version, 377);
    assert_eq!(MIGRATIONS[1].filename, "377_copy_agent_step_instances.sql");
    assert_eq!(MIGRATIONS[4].version, 380);
    assert_eq!(MIGRATIONS[4].filename, "380_copy_pipeline_definitions.sql");
    assert_eq!(MIGRATIONS[5].version, 381);
    assert_eq!(
        MIGRATIONS[5].filename,
        "381_drop_legacy_workflow_tables.sql"
    );
    assert_eq!(MIGRATIONS[6].version, 382);
    assert_eq!(
        MIGRATIONS[6].filename,
        "382_grant_gwiki_tables_to_capability.sql"
    );
    assert_eq!(MIGRATIONS[7].version, 383);
    assert_eq!(
        MIGRATIONS[7].filename,
        "383_refresh_reused_interactive_principal.sql"
    );
    assert_eq!(MIGRATIONS[8].version, 384);
    assert_eq!(
        MIGRATIONS[8].filename,
        "384_grant_projects_liveness_to_capability.sql"
    );
    assert_eq!(MIGRATIONS[9].version, 385);
    assert_eq!(
        MIGRATIONS[9].filename,
        "385_issue_maintenance_principal.sql"
    );
    assert_eq!(MIGRATIONS[10].version, 386);
    assert_eq!(
        MIGRATIONS[10].filename,
        "386_interactive_principal_role_hash.sql"
    );
    assert!(MIGRATIONS[5].sql.contains("-- gobby:destructive"));
    for migration in MIGRATIONS {
        assert_eq!(
            super::assets::sha256_hex(migration.sql.as_bytes()),
            migration.checksum
        );
    }
    assert!(
        DESTRUCTIVE_MIGRATION.version > BASELINE_VERSION
            && GUARDED_MIGRATION.version > BASELINE_VERSION,
        "injected probe versions must sit above baseline {BASELINE_VERSION}"
    );
    assert_eq!(
        super::assets::sha256_hex(DESTRUCTIVE_MIGRATION.sql.as_bytes()),
        DESTRUCTIVE_MIGRATION.checksum
    );
    assert_eq!(
        super::assets::sha256_hex(GUARDED_MIGRATION.sql.as_bytes()),
        GUARDED_MIGRATION.checksum
    );
}

#[test]
fn fresh_destructive_migration_is_receipt_stamped_without_executing() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let report =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply()?;
    assert!(report.baseline_applied);
    assert_eq!(report.migrations_applied, 1);

    let table_exists: bool = client
        .query_one("SELECT to_regclass('gate_probe') IS NOT NULL", &[])?
        .get(0);
    assert!(
        !table_exists,
        "fresh lineages must stamp destructive receipts without executing them"
    );
    let receipt_count: i64 = client
        .query_one(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 376 AND filename = $1 AND checksum = $2",
            &[&DESTRUCTIVE_MIGRATION.filename, &DESTRUCTIVE_MIGRATION.checksum],
        )?
        .get(0);
    assert_eq!(receipt_count, 1);

    let second =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply()?;
    assert!(!second.baseline_applied);
    assert_eq!(second.migrations_applied, 0);
    Ok(())
}

#[test]
fn existing_lineage_still_refuses_unauthorized_destructive_migration() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::with_migrations_for_test(&mut client, "public", &[])?.apply()?;

    let error =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply()
            .expect_err("existing lineages must still refuse unauthorized destructive migrations");
    assert!(error.to_string().contains("verified hub backup"));
    let table_exists: bool = client
        .query_one("SELECT to_regclass('gate_probe') IS NOT NULL", &[])?
        .get(0);
    assert!(!table_exists);
    Ok(())
}

#[test]
fn unauthorized_destructive_in_pending_batch_applies_nothing() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::with_migrations_for_test(&mut client, "public", &[])?.apply()?;

    assert_eq!(
        super::assets::sha256_hex(COPY_THEN_FENCE.sql.as_bytes()),
        COPY_THEN_FENCE.checksum
    );
    assert_eq!(
        super::assets::sha256_hex(DESTRUCTIVE_AFTER_COPY.sql.as_bytes()),
        DESTRUCTIVE_AFTER_COPY.checksum
    );

    let error =
        SchemaRunner::with_migrations_for_test(&mut client, "public", COPY_THEN_DESTRUCTIVE)?
            .apply()
            .expect_err("pending destructive must preflight-fail before any copy applies");
    assert!(error.to_string().contains("verified hub backup"));

    let copy_exists: bool = client
        .query_one("SELECT to_regclass('copy_probe') IS NOT NULL", &[])?
        .get(0);
    let drop_exists: bool = client
        .query_one("SELECT to_regclass('drop_probe') IS NOT NULL", &[])?
        .get(0);
    assert!(
        !copy_exists,
        "copy migration must not commit before the rejected destructive"
    );
    assert!(!drop_exists);
    assert_eq!(migration_receipt_count(&mut client, &COPY_THEN_FENCE)?, 0);
    assert_eq!(
        migration_receipt_count(&mut client, &DESTRUCTIVE_AFTER_COPY)?,
        0
    );
    Ok(())
}

#[test]
fn guarded_nondestructive_migration_applies_on_fresh_and_predecessor() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let fresh = SchemaRunner::with_migrations_for_test(&mut client, "public", GUARDED_MIGRATIONS)?
        .apply()?;
    assert!(fresh.baseline_applied);
    assert_eq!(fresh.migrations_applied, 1);
    let copied: bool = client
        .query_one(
            "SELECT to_regclass('guarded_probe_copied') IS NOT NULL",
            &[],
        )?
        .get(0);
    assert!(!copied, "absent source must keep the guarded body a no-op");
    assert_eq!(migration_receipt_count(&mut client, &GUARDED_MIGRATION)?, 1);

    let fresh_replay =
        SchemaRunner::with_migrations_for_test(&mut client, "public", GUARDED_MIGRATIONS)?
            .apply()?;
    assert!(!fresh_replay.baseline_applied);
    assert_eq!(fresh_replay.migrations_applied, 0);

    let Some((_predecessor_database, mut predecessor_client)) = test_database()? else {
        return Ok(());
    };
    install_predecessor(&mut predecessor_client)?;
    let predecessor = SchemaRunner::with_migrations_for_test(
        &mut predecessor_client,
        "public",
        GUARDED_MIGRATIONS,
    )?
    .apply()?;
    assert!(predecessor.baseline_applied);
    assert_eq!(predecessor.migrations_applied, 1);
    let copied_after_predecessor: bool = predecessor_client
        .query_one(
            "SELECT to_regclass('guarded_probe_copied') IS NOT NULL",
            &[],
        )?
        .get(0);
    assert!(!copied_after_predecessor);
    assert_eq!(
        migration_receipt_count(&mut predecessor_client, &GUARDED_MIGRATION)?,
        1
    );

    let predecessor_replay = SchemaRunner::with_migrations_for_test(
        &mut predecessor_client,
        "public",
        GUARDED_MIGRATIONS,
    )?
    .apply()?;
    assert!(!predecessor_replay.baseline_applied);
    assert_eq!(predecessor_replay.migrations_applied, 0);
    Ok(())
}

#[test]
fn copy_migrations_receipt_noop_on_fresh_final_baseline() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let first = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(first.baseline_applied);
    assert_eq!(first.migrations_applied, MIGRATIONS.len());
    let defs: bool = client
        .query_one(
            "SELECT to_regclass('workflow_definitions') IS NOT NULL",
            &[],
        )?
        .get(0);
    let inst: bool = client
        .query_one("SELECT to_regclass('workflow_instances') IS NOT NULL", &[])?
        .get(0);
    let ledger: bool = client
        .query_one("SELECT to_regclass('legacy_copy_ledger') IS NOT NULL", &[])?
        .get(0);
    assert!(!defs && !inst && !ledger);
    let agents: i64 = client
        .query_one("SELECT COUNT(*) FROM agent_definitions", &[])?
        .get(0);
    let rules: i64 = client
        .query_one("SELECT COUNT(*) FROM rule_definitions", &[])?
        .get(0);
    let variables: i64 = client
        .query_one("SELECT COUNT(*) FROM session_variable_defaults", &[])?
        .get(0);
    let pipelines: i64 = client
        .query_one("SELECT COUNT(*) FROM pipeline_definitions", &[])?
        .get(0);
    assert_eq!((agents, rules, variables, pipelines), (0, 0, 0, 0));
    for migration in MIGRATIONS {
        assert_eq!(migration_receipt_count(&mut client, migration)?, 1);
    }

    let replay = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(!replay.baseline_applied);
    assert_eq!(replay.migrations_applied, 0);
    Ok(())
}

#[test]
fn drop_migration_refused_on_predecessor_until_verified_backup() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_predecessor(&mut client)?;

    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("existing predecessor lineage must refuse the destructive drop");
    assert!(error.to_string().contains("verified hub backup"));
    let defs_before: bool = client
        .query_one(
            "SELECT to_regclass('workflow_definitions') IS NOT NULL",
            &[],
        )?
        .get(0);
    assert!(defs_before);

    let fixture = include_str!("../../tests/fixtures/hub_backup_manifest/v2_roundtrip.json");
    let mut manifest = parse_backup_manifest(fixture)?;
    let database_head: i32 = client
        .query_one(
            "SELECT COALESCE(MAX(version), 0)::int FROM schema_migrations",
            &[],
        )?
        .get(0);
    manifest.backup_starting_head = database_head;
    let root = env::temp_dir().join(format!("gcore-drop-gate-{}", Uuid::new_v4()));
    let _scratch_path = ScratchPath(root.clone());
    fs::create_dir_all(root.join("postgres"))?;
    fs::write(root.join("postgres/fixture.dump"), [])?;
    manifest.artifacts[0].sha256 =
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_owned();
    let identity = source_identity(&mut client)?;
    manifest.source_identity = identity.clone();
    let created_at = OffsetDateTime::parse(&manifest.created_at, &Rfc3339)?;
    let mut context = BackupGateContext::new(
        &root,
        &identity,
        database_head,
        created_at + Duration::hours(1),
    );
    context.max_age = Duration::hours(2);
    let verified = VerifiedBackupManifest::verify(manifest, &context)?;

    let report = SchemaRunner::new(&mut client, "public")?.apply_with_backup(&verified)?;
    assert_eq!(report.migrations_applied, MIGRATIONS.len());
    let defs_after: bool = client
        .query_one(
            "SELECT to_regclass('workflow_definitions') IS NOT NULL",
            &[],
        )?
        .get(0);
    let inst_after: bool = client
        .query_one("SELECT to_regclass('workflow_instances') IS NOT NULL", &[])?
        .get(0);
    let ledger_after: bool = client
        .query_one("SELECT to_regclass('legacy_copy_ledger') IS NOT NULL", &[])?
        .get(0);
    assert!(!defs_after && !inst_after && !ledger_after);
    Ok(())
}

fn migration_receipt_count(
    client: &mut Client,
    migration: &EmbeddedMigration,
) -> anyhow::Result<i64> {
    Ok(client
        .query_one(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = $1 AND filename = $2 AND checksum = $3",
            &[&migration.version, &migration.filename, &migration.checksum],
        )?
        .get(0))
}
