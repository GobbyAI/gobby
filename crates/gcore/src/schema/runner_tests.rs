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
use super::external::{ExternalPostgresObjectKind, gcode_postgres_objects};
use super::gate::{
    BackupGateContext, SourceIdentity, VerifiedBackupManifest, parse_backup_manifest,
};
use super::runner::{
    ACCOUNT_IDENTITY_PREDECESSOR_CHECKSUM, PARENT_BASELINE_CHECKSUM, PREDECESSOR_BASELINE_CHECKSUM,
    SchemaRunner, WORKTREE_BASELINE_CHECKSUM,
};
use super::sql_splitter::split_sql_statements;
use super::verify::catalog_manifest;

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

const GCODE_RLS_TABLES: [&str; 11] = [
    "code_indexed_projects",
    "code_indexed_project_states",
    "code_indexed_file_states",
    "code_indexed_files",
    "code_symbols",
    "code_imports",
    "code_calls",
    "code_inheritance",
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
fn fresh_baseline_grants_terminals_to_daemon_runtime() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    install_baseline(&mut client)?;
    assert_runtime_crud_privileges(&mut client, "terminals")?;
    let gcode_select: bool = client
        .query_one(
            "SELECT has_table_privilege('gobby_gcode_capability', 'terminals', 'SELECT')",
            &[],
        )?
        .get(0);
    assert!(!gcode_select, "scoped gcode must not SELECT terminals");
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
        "UPDATE schema_migrations SET checksum = $1 WHERE version = $2",
        &[
            &"ece3754752dbc72aaff4bbd3ebaa91a41305e4899e180012f8429c4f7467b1bf",
            &BASELINE_VERSION,
        ],
    )?;
    let prior_baseline = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(!prior_baseline.baseline_applied);
    assert_eq!(prior_baseline.migrations_applied, 0);
    let kept_prior: String = client
        .query_one(
            "SELECT checksum FROM schema_migrations WHERE version = $1",
            &[&BASELINE_VERSION],
        )?
        .get(0);
    assert_eq!(
        kept_prior,
        "ece3754752dbc72aaff4bbd3ebaa91a41305e4899e180012f8429c4f7467b1bf"
    );

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
    let refreshed_tool_issuer: String = client
        .query_one(
            "SELECT pg_get_functiondef( \
                 'gobby_agent_auth.issue_tool_principal(uuid,uuid,uuid,timestamptz,text)'::regprocedure \
             )",
            &[],
        )?
        .get(0);
    assert!(refreshed_tool_issuer.contains("agent_session_id"));
    assert!(refreshed_tool_issuer.contains("code_overlay_project_id"));
    let issuer_can_read_workspace_session: bool = client
        .query_one(
            "SELECT has_column_privilege( \
                 'gobby_agent_issuer', 'worktrees', 'agent_session_id', 'SELECT' \
             ) AND has_column_privilege( \
                 'gobby_agent_issuer', 'clones', 'agent_session_id', 'SELECT' \
             )",
            &[],
        )?
        .get(0);
    assert!(issuer_can_read_workspace_session);
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
fn prior_current_baseline_refreshes_tool_chat_overlay_issuer() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_parent_baseline(&mut client)?;
    client.execute(
        "UPDATE schema_migrations SET checksum = $1 WHERE version = $2",
        &[
            &super::assets::TOOL_CHAT_OVERLAY_PREDECESSOR_CHECKSUM,
            &BASELINE_VERSION,
        ],
    )?;
    let before: String = client
        .query_one(
            "SELECT pg_get_functiondef( \
                 'gobby_agent_auth.issue_tool_principal(uuid,uuid,uuid,timestamptz,text)'::regprocedure \
             )",
            &[],
        )?
        .get(0);
    assert!(!before.contains("agent_session_id"));

    let report = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(report.baseline_applied);
    let after: String = client
        .query_one(
            "SELECT pg_get_functiondef( \
                 'gobby_agent_auth.issue_tool_principal(uuid,uuid,uuid,timestamptz,text)'::regprocedure \
             )",
            &[],
        )?
        .get(0);
    assert!(after.contains("agent_session_id"));
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
    client.execute(
        "UPDATE schema_migrations SET checksum = $1 WHERE version = $2",
        &[
            &super::assets::WORKTREE_PRE_OVERLAY_BASELINE_CHECKSUM,
            &BASELINE_VERSION,
        ],
    )?;
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
fn interactive_principal_binds_only_registered_worktree_overlays() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;

    let machine_id = Uuid::new_v4();
    let owner_user_id = Uuid::new_v4();
    let project_id = Uuid::new_v4();
    let unrelated_project_id = Uuid::new_v4();
    let session_id = Uuid::new_v4();
    let deployment_token = format!("ix-overlay-{}", Uuid::new_v4().simple());
    let main_password = format!("gobby-ix-main-{}", Uuid::new_v4().simple());
    let overlay_password = format!("gobby-ix-overlay-{}", Uuid::new_v4().simple());

    client.execute(
        "INSERT INTO users(id, email, name, password_hash) \
         VALUES ($1, 'schema-ix@example.invalid', 'Schema Interactive', 'test-only')",
        &[&owner_user_id],
    )?;
    client.execute(
        "INSERT INTO machines(id, hostname, owner_user_id) VALUES ($1, 'schema-ix', $2)",
        &[&machine_id, &owner_user_id],
    )?;
    client.execute(
        "INSERT INTO projects(id, name, repo_path) \
         VALUES ($1, 'parent', '/tmp/gobby-ix-parent'), ($2, 'unrelated', '/tmp/gobby-ix-unrelated')",
        &[&project_id, &unrelated_project_id],
    )?;
    client.execute(
        "INSERT INTO sessions(id, external_id, machine_id, source, project_id) \
         VALUES ($1, 'schema-interactive', $2, 'test', $3)",
        &[&session_id, &machine_id, &project_id],
    )?;
    client.execute(
        "INSERT INTO worktrees(id, project_id, machine_id, branch_name, worktree_path) \
         VALUES ($1, $2, $3, 'schema-ix', '/tmp/gobby-ix-worktree')",
        &[&Uuid::new_v4(), &project_id, &machine_id],
    )?;
    let overlay_project_id: Uuid = client
        .query_one(
            "SELECT gobby_agent_auth.code_index_project_id('/tmp/gobby-ix-worktree')",
            &[],
        )?
        .get(0);
    let unregistered_overlay_id: Uuid = client
        .query_one(
            "SELECT gobby_agent_auth.code_index_project_id('/tmp/gobby-ix-elsewhere')",
            &[],
        )?
        .get(0);

    const ISSUE_SQL: &str = "SELECT role_name::TEXT, credential_generation, reused \
         FROM gobby_agent_auth.issue_or_reuse_interactive_principal( \
             $1, $2, $3, $4, clock_timestamp() + INTERVAL '10 minutes', $5, $6 \
         )";
    let main_row = client.query_one(
        ISSUE_SQL,
        &[
            &deployment_token,
            &machine_id,
            &project_id,
            &session_id,
            &main_password,
            &None::<Uuid>,
        ],
    )?;
    let main_role: String = main_row.get(0);
    assert!(!main_row.get::<_, bool>(2));
    let overlay_row = client.query_one(
        ISSUE_SQL,
        &[
            &deployment_token,
            &machine_id,
            &project_id,
            &session_id,
            &overlay_password,
            &Some(overlay_project_id),
        ],
    )?;
    let overlay_role: String = overlay_row.get(0);
    assert!(!overlay_row.get::<_, bool>(2));
    assert_ne!(
        main_role, overlay_role,
        "main and overlay callers must not share a role"
    );
    assert_ne!(
        main_row.get::<_, i32>(1),
        overlay_row.get::<_, i32>(1),
        "generations stay unique per project"
    );

    let reused_overlay = client.query_one(
        ISSUE_SQL,
        &[
            &deployment_token,
            &machine_id,
            &project_id,
            &session_id,
            &"ignored-on-reuse",
            &Some(overlay_project_id),
        ],
    )?;
    assert_eq!(reused_overlay.get::<_, String>(0), overlay_role);
    assert!(
        reused_overlay.get::<_, bool>(2),
        "same overlay must reuse its binding"
    );
    let reused_main = client.query_one(
        ISSUE_SQL,
        &[
            &deployment_token,
            &machine_id,
            &project_id,
            &session_id,
            &"ignored-on-reuse",
            &None::<Uuid>,
        ],
    )?;
    assert_eq!(reused_main.get::<_, String>(0), main_role);
    assert!(
        reused_main.get::<_, bool>(2),
        "main checkout must reuse its own binding"
    );

    let bound_overlays: Vec<(String, Option<Uuid>)> = client
        .query(
            "SELECT role_name::TEXT, code_overlay_project_id \
             FROM gobby_agent_auth.principal_bindings \
             WHERE owner_kind = 'interactive' AND deployment_token = $1 \
             ORDER BY credential_generation",
            &[&deployment_token],
        )?
        .into_iter()
        .map(|row| (row.get(0), row.get(1)))
        .collect();
    assert_eq!(
        bound_overlays,
        vec![
            (main_role.clone(), None),
            (overlay_role.clone(), Some(overlay_project_id)),
        ]
    );

    let error = client
        .query_one(
            ISSUE_SQL,
            &[
                &deployment_token,
                &machine_id,
                &project_id,
                &session_id,
                &"unregistered",
                &Some(unregistered_overlay_id),
            ],
        )
        .expect_err("an unregistered overlay must be refused");
    assert_eq!(
        error.as_db_error().map(|db| db.code()),
        Some(&SqlState::FOREIGN_KEY_VIOLATION)
    );
    let error = client
        .query_one(
            ISSUE_SQL,
            &[
                &deployment_token,
                &machine_id,
                &project_id,
                &session_id,
                &"self-overlay",
                &Some(project_id),
            ],
        )
        .expect_err("the parent project is not an overlay");
    assert_eq!(
        error.as_db_error().map(|db| db.code()),
        Some(&SqlState::INVALID_PARAMETER_VALUE)
    );

    client.execute(
        "INSERT INTO code_indexed_projects(id) VALUES ($1)",
        &[&project_id],
    )?;

    let validation = (|| -> anyhow::Result<()> {
        let mut scoped = database.connect_as(&overlay_role, &overlay_password)?;
        let visible_parent: bool = scoped
            .query_one(
                "SELECT EXISTS(SELECT 1 FROM code_indexed_projects WHERE id = $1)",
                &[&project_id],
            )?
            .get(0);
        assert!(
            visible_parent,
            "overlay principal must read the parent index"
        );
        scoped.execute(
            "INSERT INTO code_indexed_projects(id) VALUES ($1)",
            &[&overlay_project_id],
        )?;
        scoped.execute(
            "INSERT INTO code_indexed_project_states( \
                 machine_id, project_id, root_path, total_files, total_symbols \
             ) VALUES ($1, $2, '/tmp/gobby-ix-worktree', 1, 0)",
            &[&machine_id, &overlay_project_id],
        )?;
        assert_eq!(
            scoped.execute(
                "UPDATE code_indexed_projects SET updated_at = NOW() WHERE id = $1",
                &[&project_id],
            )?,
            0,
            "overlay principal must not update parent facts",
        );
        let error = scoped
            .execute(
                "INSERT INTO code_indexed_projects(id) VALUES ($1)",
                &[&unrelated_project_id],
            )
            .expect_err("overlay principal must not write unrelated projects");
        assert_eq!(error.code(), Some(&SqlState::INSUFFICIENT_PRIVILEGE));
        drop(scoped);

        let mut main_scoped = database.connect_as(&main_role, &main_password)?;
        let visible_projects: i64 = main_scoped
            .query_one("SELECT count(*) FROM code_indexed_projects", &[])?
            .get(0);
        assert_eq!(
            visible_projects, 1,
            "main checkout must not see the overlay"
        );
        let error = main_scoped
            .execute(
                "INSERT INTO code_indexed_project_states( \
                     machine_id, project_id, root_path, total_files, total_symbols \
                 ) VALUES ($1, $2, '/tmp/gobby-ix-worktree', 1, 0)",
                &[&machine_id, &overlay_project_id],
            )
            .expect_err("main checkout principal must not write the overlay");
        assert_eq!(error.code(), Some(&SqlState::INSUFFICIENT_PRIVILEGE));
        Ok(())
    })();

    let rotated = client.query_one(
        "SELECT role_name::TEXT, credential_generation \
         FROM gobby_agent_auth.rotate_interactive_principal( \
             $1, $2, $3, $4, clock_timestamp() + INTERVAL '10 minutes', $5, \
             clock_timestamp() + INTERVAL '1 minute', $6 \
         )",
        &[
            &deployment_token,
            &machine_id,
            &project_id,
            &session_id,
            &"rotated-overlay",
            &Some(overlay_project_id),
        ],
    );
    validation?;
    let rotated = rotated?;
    let rotated_role: String = rotated.get(0);
    let rotated_overlay: Option<Uuid> = client
        .query_one(
            "SELECT code_overlay_project_id FROM gobby_agent_auth.principal_bindings \
             WHERE role_name = $1",
            &[&rotated_role],
        )?
        .get(0);
    assert_eq!(rotated_overlay, Some(overlay_project_id));
    let draining_main: bool = client
        .query_one(
            "SELECT predecessor_drain_deadline IS NOT NULL \
             FROM gobby_agent_auth.principal_bindings WHERE role_name = $1",
            &[&main_role],
        )?
        .get(0);
    assert!(
        !draining_main,
        "rotating the overlay must leave the main binding alone"
    );
    assert_gcode_rls_policies(&mut client)?;
    Ok(())
}

#[test]
fn tool_chat_principal_reads_parent_and_writes_only_its_worktree_overlay() -> anyhow::Result<()> {
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
    let session_id = Uuid::new_v4();
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
         VALUES ($1, 'schema-tool-chat', $2, 'test', $3)",
        &[&session_id, &machine_id, &project_id],
    )?;
    client.execute(
        "INSERT INTO worktrees( \
             id, project_id, machine_id, branch_name, worktree_path, agent_session_id \
         ) VALUES ($1, $2, $3, 'schema-test', '/tmp/gobby-rls-overlay', $4)",
        &[&worktree_id, &project_id, &machine_id, &session_id],
    )?;

    let role_name: String = client
        .query_one(
            "SELECT role_name::TEXT FROM gobby_agent_auth.issue_tool_principal( \
                 $1, $2, $3, clock_timestamp() + INTERVAL '10 minutes', $4 \
             )",
            &[&execution_id, &session_id, &machine_id, &password],
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
            let other_overlay_id = Uuid::new_v4();
            let error = scoped
                .execute(
                    "INSERT INTO code_indexed_projects(id) VALUES ($1)",
                    &[&other_overlay_id],
                )
                .expect_err("writes to another overlay must fail RLS");
            assert_eq!(error.code(), Some(&SqlState::INSUFFICIENT_PRIVILEGE));
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
fn tool_chat_principal_binding_handles_clone_parent_and_ambiguous_workspaces() -> anyhow::Result<()>
{
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;

    let owner_user_id = Uuid::new_v4();
    let machine_id = Uuid::new_v4();
    let project_id = Uuid::new_v4();
    let clone_session_id = Uuid::new_v4();
    let parent_session_id = Uuid::new_v4();
    let ambiguous_session_id = Uuid::new_v4();
    client.execute(
        "INSERT INTO users(id, email, name, password_hash) \
         VALUES ($1, 'tool-binding@example.invalid', 'Tool Binding', 'test-only')",
        &[&owner_user_id],
    )?;
    client.execute(
        "INSERT INTO machines(id, hostname, owner_user_id) VALUES ($1, 'tool-binding', $2)",
        &[&machine_id, &owner_user_id],
    )?;
    client.execute(
        "INSERT INTO projects(id, name, repo_path) \
         VALUES ($1, 'tool-binding', '/tmp/gobby-tool-binding')",
        &[&project_id],
    )?;
    client.execute(
        "INSERT INTO sessions(id, external_id, machine_id, source, project_id) VALUES \
             ($1, 'tool-clone', $4, 'test', $5), \
             ($2, 'tool-parent', $4, 'test', $5), \
             ($3, 'tool-ambiguous', $4, 'test', $5)",
        &[
            &clone_session_id,
            &parent_session_id,
            &ambiguous_session_id,
            &machine_id,
            &project_id,
        ],
    )?;
    client.execute(
        "INSERT INTO clones( \
             id, project_id, machine_id, branch_name, clone_path, agent_session_id \
         ) VALUES ($1, $2, $3, 'clone', '/tmp/gobby-tool-clone', $4), \
                  ($5, $2, $3, 'ambiguous-clone', '/tmp/gobby-ambiguous-clone', $6)",
        &[
            &Uuid::new_v4(),
            &project_id,
            &machine_id,
            &clone_session_id,
            &Uuid::new_v4(),
            &ambiguous_session_id,
        ],
    )?;
    client.execute(
        "INSERT INTO worktrees( \
             id, project_id, machine_id, branch_name, worktree_path, agent_session_id \
         ) VALUES ($1, $2, $3, 'ambiguous-worktree', '/tmp/gobby-ambiguous-worktree', $4)",
        &[
            &Uuid::new_v4(),
            &project_id,
            &machine_id,
            &ambiguous_session_id,
        ],
    )?;

    let clone_execution_id = Uuid::new_v4();
    let clone_password = format!("gobby-tool-clone-{}", Uuid::new_v4().simple());
    client.query_one(
        "SELECT role_name FROM gobby_agent_auth.issue_tool_principal( \
             $1, $2, $3, clock_timestamp() + INTERVAL '10 minutes', $4 \
         )",
        &[
            &clone_execution_id,
            &clone_session_id,
            &machine_id,
            &clone_password,
        ],
    )?;
    let clone_overlay: Option<Uuid> = client
        .query_one(
            "SELECT code_overlay_project_id FROM gobby_agent_auth.principal_bindings \
             WHERE managed_execution_id = $1",
            &[&clone_execution_id],
        )?
        .get(0);
    let expected_clone_overlay: Uuid = client
        .query_one(
            "SELECT gobby_agent_auth.code_index_project_id('/tmp/gobby-tool-clone')",
            &[],
        )?
        .get(0);
    assert_eq!(clone_overlay, Some(expected_clone_overlay));
    client.query_one(
        "SELECT gobby_agent_auth.revoke_principal($1, 1)",
        &[&clone_execution_id],
    )?;

    let parent_execution_id = Uuid::new_v4();
    let parent_password = format!("gobby-tool-parent-{}", Uuid::new_v4().simple());
    client.query_one(
        "SELECT role_name FROM gobby_agent_auth.issue_tool_principal( \
             $1, $2, $3, clock_timestamp() + INTERVAL '10 minutes', $4 \
         )",
        &[
            &parent_execution_id,
            &parent_session_id,
            &machine_id,
            &parent_password,
        ],
    )?;
    let parent_overlay: Option<Uuid> = client
        .query_one(
            "SELECT code_overlay_project_id FROM gobby_agent_auth.principal_bindings \
             WHERE managed_execution_id = $1",
            &[&parent_execution_id],
        )?
        .get(0);
    assert_eq!(parent_overlay, None);
    client.query_one(
        "SELECT gobby_agent_auth.revoke_principal($1, 1)",
        &[&parent_execution_id],
    )?;

    let ambiguous_error = client
        .query_one(
            "SELECT role_name FROM gobby_agent_auth.issue_tool_principal( \
                 $1, $2, $3, clock_timestamp() + INTERVAL '10 minutes', $4 \
             )",
            &[
                &Uuid::new_v4(),
                &ambiguous_session_id,
                &machine_id,
                &format!("gobby-tool-ambiguous-{}", Uuid::new_v4().simple()),
            ],
        )
        .expect_err("multiple session workspaces must fail closed");
    assert_eq!(ambiguous_error.code(), Some(&SqlState::CHECK_VIOLATION));
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
fn lock_and_recovery_tests_database_apply_lock_serializes_schemas() -> anyhow::Result<()> {
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
        "SELECT pg_advisory_lock(hashtext('postgres_migrations_apply'), 0)",
        &[],
    )?;

    let (sender, receiver) = mpsc::channel();
    let worker = std::thread::spawn(move || {
        let result = SchemaRunner::new(&mut apply_client, "schema_lock_b")
            .and_then(|mut runner| runner.apply())
            .map_err(|error| error.to_string());
        sender.send(result).expect("receiver remains available");
    });
    // The baseline creates cluster-global roles and the shared gobby_agent_auth
    // objects, so applying another schema must wait for the database-wide lock.
    assert!(
        matches!(
            receiver.recv_timeout(StdDuration::from_secs(2)),
            Err(mpsc::RecvTimeoutError::Timeout)
        ),
        "apply for another schema must block while the database apply lock is held"
    );
    lock_client.query_one(
        "SELECT pg_advisory_unlock(hashtext('postgres_migrations_apply'), 0)",
        &[],
    )?;
    let result = receiver
        .recv_timeout(StdDuration::from_secs(30))
        .expect("apply must proceed once the database apply lock is released");
    worker.join().expect("schema worker must not panic");
    result.map_err(anyhow::Error::msg)?;
    Ok(())
}

#[test]
fn lock_and_recovery_tests_failed_apply_releases_database_apply_lock() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;
    client.execute(
        "UPDATE schema_migrations SET checksum = 'unrecognized' WHERE version = $1",
        &[&BASELINE_VERSION],
    )?;

    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("a corrupt receipt must fail inside the locked apply section");
    assert!(
        error
            .to_string()
            .contains("recreate from a verified backup"),
        "failure must come from apply_locked, got: {error}"
    );

    // The session-level lock lives on the runner's connection, so a fresh
    // connection can only take it if the failed apply released it.
    let mut probe = database.connect()?;
    let acquired: bool = probe
        .query_one(
            "SELECT pg_try_advisory_lock(hashtext('postgres_migrations_apply'), 0)",
            &[],
        )?
        .get(0);
    assert!(
        acquired,
        "a failed apply must release the database apply lock"
    );
    probe.query_one(
        "SELECT pg_advisory_unlock(hashtext('postgres_migrations_apply'), 0)",
        &[],
    )?;
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

    let fixture = include_str!("../../tests/fixtures/hub_backup_manifest/v3_roundtrip.json");
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
    assert_eq!(MIGRATIONS.len(), 33);
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
    assert_eq!(MIGRATIONS[11].version, 387);
    assert_eq!(
        MIGRATIONS[11].filename,
        "387_interactive_principal_role_helper.sql"
    );
    assert_eq!(MIGRATIONS[12].version, 388);
    assert_eq!(
        MIGRATIONS[12].filename,
        "388_grant_interactive_role_name.sql"
    );
    assert_eq!(MIGRATIONS[13].version, 389);
    assert_eq!(
        MIGRATIONS[13].filename,
        "389_sweep_interactive_orphan_roles.sql"
    );
    assert_eq!(MIGRATIONS[14].version, 390);
    assert_eq!(
        MIGRATIONS[14].filename,
        "390_retain_interactive_credential_material.sql"
    );
    assert_eq!(MIGRATIONS[15].version, 391);
    assert_eq!(
        MIGRATIONS[15].filename,
        "391_session_last_activity_and_creation_defaults.sql"
    );
    assert_eq!(MIGRATIONS[16].version, 392);
    assert_eq!(
        MIGRATIONS[16].filename,
        "392_chat_attachments_deletion_lease.sql"
    );
    assert_eq!(MIGRATIONS[17].version, 393);
    assert_eq!(
        MIGRATIONS[17].filename,
        "393_interactive_principal_hardening.sql"
    );
    assert_eq!(MIGRATIONS[18].version, 394);
    assert_eq!(
        MIGRATIONS[18].filename,
        "394_sessions_status_last_activity_index.sql"
    );
    assert_eq!(MIGRATIONS[19].version, 395);
    assert_eq!(MIGRATIONS[19].filename, "395_code_inheritance.sql");
    assert_eq!(MIGRATIONS[20].version, 396);
    assert_eq!(
        MIGRATIONS[20].filename,
        "396_memory_rationale_and_provenance.sql"
    );
    assert_eq!(MIGRATIONS[21].version, 397);
    assert_eq!(
        MIGRATIONS[21].filename,
        "397_memories_source_task_index.sql"
    );
    assert_eq!(MIGRATIONS[22].version, 398);
    assert_eq!(
        MIGRATIONS[22].filename,
        "398_code_indexed_project_states_indexer_version.sql"
    );
    assert_eq!(MIGRATIONS[23].version, 399);
    assert_eq!(
        MIGRATIONS[23].filename,
        "399_drain_orphan_binding_alias.sql"
    );
    assert_eq!(MIGRATIONS[24].version, 400);
    assert_eq!(
        MIGRATIONS[24].filename,
        "400_drop_vision_extract_config_rows.sql"
    );
    assert_eq!(MIGRATIONS[25].version, 401);
    assert_eq!(MIGRATIONS[25].filename, "401_model_metadata_reasoning.sql");
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

    let fixture = include_str!("../../tests/fixtures/hub_backup_manifest/v3_roundtrip.json");
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

#[test]
fn code_inheritance_has_gcode_project_policies() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    assert_gcode_rls_policies(&mut client)?;

    let owner_user_id = Uuid::new_v4();
    let machine_id = Uuid::new_v4();
    let project_id = Uuid::new_v4();
    let other_project_id = Uuid::new_v4();
    let parent_session_id = Uuid::new_v4();
    let child_session_id = Uuid::new_v4();
    let agent_run_id = Uuid::new_v4();
    let worktree_id = Uuid::new_v4();
    let execution_id = Uuid::new_v4();
    let password = format!("gobby-inheritance-rls-{}", Uuid::new_v4().simple());

    client.execute(
        "INSERT INTO users(id, email, name, password_hash) \
         VALUES ($1, 'inheritance-rls@example.invalid', 'Inheritance RLS', 'test-only')",
        &[&owner_user_id],
    )?;
    client.execute(
        "INSERT INTO machines(id, hostname, owner_user_id) VALUES ($1, 'inheritance-rls', $2)",
        &[&machine_id, &owner_user_id],
    )?;
    client.execute(
        "INSERT INTO projects(id, name, repo_path) \
         VALUES ($1, 'parent', '/tmp/gobby-inheritance'), ($2, 'other', '/tmp/other')",
        &[&project_id, &other_project_id],
    )?;
    client.execute(
        "INSERT INTO sessions(id, external_id, machine_id, source, project_id) \
         VALUES ($1, 'inheritance-parent', $2, 'test', $3)",
        &[&parent_session_id, &machine_id, &project_id],
    )?;
    client.execute(
        "INSERT INTO worktrees(id, project_id, machine_id, branch_name, worktree_path) \
         VALUES ($1, $2, $3, 'inheritance-rls', '/tmp/gobby-inheritance')",
        &[&worktree_id, &project_id, &machine_id],
    )?;
    client.execute(
        "INSERT INTO agent_runs(id, machine_id, parent_session_id, provider, prompt, worktree_id) \
         VALUES ($1, $2, $3, 'test', 'inheritance rls', $4)",
        &[&agent_run_id, &machine_id, &parent_session_id, &worktree_id],
    )?;
    client.execute(
        "INSERT INTO sessions( \
             id, external_id, machine_id, source, project_id, parent_session_id, agent_run_id \
         ) VALUES ($1, 'inheritance-child', $2, 'test', $3, $4, $5)",
        &[
            &child_session_id,
            &machine_id,
            &project_id,
            &parent_session_id,
            &agent_run_id,
        ],
    )?;
    client.execute(
        "INSERT INTO code_indexed_projects(id) VALUES ($1), ($2)",
        &[&project_id, &other_project_id],
    )?;
    client.execute(
        "INSERT INTO code_indexed_files(id, project_id, file_path, language, content_hash) \
         VALUES ($1, $2, 'a.rs', 'rust', 'hash-a'), ($3, $4, 'b.rs', 'rust', 'hash-b')",
        &[
            &Uuid::new_v4(),
            &project_id,
            &Uuid::new_v4(),
            &other_project_id,
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
    let write_project_id = bound_overlay_id.unwrap_or(project_id);
    if bound_overlay_id.is_some() {
        client.execute(
            "INSERT INTO code_indexed_projects(id) VALUES ($1)",
            &[&write_project_id],
        )?;
        client.execute(
            "INSERT INTO code_indexed_files(id, project_id, file_path, language, content_hash) \
             VALUES ($1, $2, 'a.rs', 'rust', 'hash-a')",
            &[&Uuid::new_v4(), &write_project_id],
        )?;
    }

    let validation = match database.connect_as(&role_name, &password) {
        Ok(mut scoped) => (|| -> anyhow::Result<()> {
            scoped.execute(
                "INSERT INTO code_inheritance(\
                     project_id, source_name, target_name, heritage_kind, file_path, content_hash, line\
                 ) VALUES ($1, 'Derived', 'Base', 'EXTENDS', 'a.rs', 'hash-a', 1)",
                &[&write_project_id],
            )?;
            let visible: i64 = scoped
                .query_one("SELECT count(*) FROM code_inheritance", &[])?
                .get(0);
            assert_eq!(visible, 1, "same-project inheritance rows must be readable");
            let error = scoped
                .execute(
                    "INSERT INTO code_inheritance(\
                         project_id, source_name, target_name, heritage_kind, file_path, content_hash, line\
                     ) VALUES ($1, 'Other', 'Base', 'EXTENDS', 'b.rs', 'hash-b', 1)",
                    &[&other_project_id],
                )
                .expect_err("cross-project inheritance inserts must fail RLS");
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
    Ok(())
}

#[test]
fn code_inheritance_heritage_kind_check_rejects_unknown() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    let project_id = Uuid::new_v4();
    client.execute(
        "INSERT INTO code_indexed_projects(id) VALUES ($1)",
        &[&project_id],
    )?;
    client.execute(
        "INSERT INTO code_indexed_files(id, project_id, file_path, language, content_hash) \
         VALUES ($1, $2, 'a.rs', 'rust', 'hash-a')",
        &[&Uuid::new_v4(), &project_id],
    )?;
    for kind in ["INHERITS", "EXTENDS", "IMPLEMENTS"] {
        client.execute(
            "INSERT INTO code_inheritance(\
                 project_id, source_name, target_name, heritage_kind, file_path, content_hash, line\
             ) VALUES ($1, 'Derived', 'Base', $2, 'a.rs', 'hash-a', 1)",
            &[&project_id, &kind],
        )?;
    }
    let error = client
        .execute(
            "INSERT INTO code_inheritance(\
                 project_id, source_name, target_name, heritage_kind, file_path, content_hash, line\
             ) VALUES ($1, 'Derived', 'Base', 'MIXIN', 'a.rs', 'hash-a', 2)",
            &[&project_id],
        )
        .expect_err("unknown heritage_kind must fail the CHECK");
    assert_eq!(error.code(), Some(&SqlState::CHECK_VIOLATION));
    Ok(())
}

#[test]
fn code_inheritance_is_in_gcode_postgres_objects() {
    let objects = gcode_postgres_objects("public").expect("external objects");
    let names: BTreeSet<&str> = objects.iter().map(|object| object.name).collect();
    assert!(names.contains("code_inheritance table"));
    assert!(names.contains("idx_cinherit_file index"));
    assert!(names.contains("idx_cinherit_source index"));
    assert!(names.contains("idx_cinherit_target index"));
    let table = objects
        .iter()
        .find(|object| object.name == "code_inheritance table")
        .expect("code_inheritance table object");
    assert_eq!(table.kind, ExternalPostgresObjectKind::Table);
    assert!(table.sql.contains("code_inheritance_unique_target"));
    assert!(table.sql.contains("REFERENCES"));
    assert!(table.sql.contains("ON DELETE CASCADE"));
    assert!(!table.sql.contains("idx_cinherit_file"));
}

#[test]
fn code_inheritance_adoption_preserves_pre_inheritance_and_skips_existing() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    for object in gcode_postgres_objects("public")? {
        if object.name.contains("inheritance") || object.name.contains("cinherit") {
            continue;
        }
        client.batch_execute(&object.sql)?;
    }
    let before: bool = client
        .query_one("SELECT to_regclass('code_inheritance') IS NOT NULL", &[])?
        .get(0);
    assert!(!before, "pre-inheritance schema must omit code_inheritance");
    SchemaRunner::new(&mut client, "public")?.apply()?;
    let after: bool = client
        .query_one("SELECT to_regclass('code_inheritance') IS NOT NULL", &[])?
        .get(0);
    assert!(after, "adoption must apply the code_inheritance hop");

    let Some((_database2, mut existing)) = test_database()? else {
        return Ok(());
    };
    for object in gcode_postgres_objects("public")? {
        existing.batch_execute(&object.sql)?;
    }
    SchemaRunner::new(&mut existing, "public")?.apply()?;
    let still: bool = existing
        .query_one("SELECT to_regclass('code_inheritance') IS NOT NULL", &[])?
        .get(0);
    assert!(still, "already-provisioned code_inheritance must be kept");
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

#[test]
fn migration_408_on_a_407_hub_matches_a_fresh_apply() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let through_407 = &MIGRATIONS[..MIGRATIONS.len() - 1];
    assert_eq!(
        through_407.last().map(|migration| migration.version),
        Some(407)
    );
    let hub =
        SchemaRunner::with_migrations_for_test(&mut client, "public", through_407)?.apply()?;
    assert!(hub.baseline_applied);
    assert_eq!(hub.migrations_applied, through_407.len());
    let legacy_columns: Vec<String> = client
        .query(
            "SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'agent_runs'
               AND column_name IN ('tmux_session_name', 'terminal_id')",
            &[],
        )?
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(legacy_columns, ["tmux_session_name"]);

    let upgraded = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(!upgraded.baseline_applied);
    assert_eq!(upgraded.migrations_applied, 1);
    let repeat = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert_eq!(repeat.migrations_applied, 0);

    let fresh = SchemaRunner::new(&mut client, "fresh_408")?.apply()?;
    assert!(fresh.baseline_applied);
    assert_eq!(fresh.migrations_applied, MIGRATIONS.len());

    assert_eq!(
        catalog_manifest(&mut client, "public")?,
        catalog_manifest(&mut client, "fresh_408")?
    );
    SchemaRunner::new(&mut client, "public")?.verify()?;
    Ok(())
}
