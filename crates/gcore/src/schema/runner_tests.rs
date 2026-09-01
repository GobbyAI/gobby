use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::sync::{Mutex, mpsc};
use std::time::Duration as StdDuration;

use postgres::{Client, Config, NoTls, error::SqlState};
use time::format_description::well_known::Rfc3339;
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use super::assets::{BASELINE_CHECKSUM, BASELINE_VERSION, EmbeddedMigration, MIGRATIONS};
use super::error::SchemaError;
use super::external::{ExternalPostgresObjectKind, gcode_postgres_objects};
use super::gate::{
    BackupGateContext, SourceIdentity, VerifiedBackupManifest, parse_backup_manifest,
};
use super::runner::{SchemaRunner, auth_schema_for, render_sql_for_schema};

static RECOVERY_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 421,
    filename: "421_recovery_probe.sql",
    checksum: "d63e14df78da3519a30caf2dac74341ab5f0c9aa05f7bec58174ec0adf383159",
    sql: "-- gobby:non-transactional\nCREATE UNIQUE INDEX CONCURRENTLY schema_recovery_idx ON recovery_values(id);\n",
};
static RECOVERY_MIGRATIONS: &[EmbeddedMigration] = &[RECOVERY_MIGRATION];

static DESTRUCTIVE_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 421,
    filename: "421_destructive_probe.sql",
    checksum: "c10820fc8be4c2bceab1610fd8372c8d864fd7c4a8985773cf903bae450b19e9",
    sql: "-- gobby:destructive\nCREATE TABLE gate_probe (id integer);\n",
};
static DESTRUCTIVE_MIGRATIONS: &[EmbeddedMigration] = &[DESTRUCTIVE_MIGRATION];

static GUARDED_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 421,
    filename: "421_guarded_probe.sql",
    checksum: "8d86f80f785ac4f918ce34ea7f0dca860266e91dbba95d8a2be0965a9cdd147a",
    sql: "DO $guard$\nBEGIN\n  IF to_regclass('legacy_probe_source') IS NOT NULL THEN\n    CREATE TABLE IF NOT EXISTS guarded_probe_copied (id integer);\n  END IF;\nEND\n$guard$;\n",
};
static GUARDED_MIGRATIONS: &[EmbeddedMigration] = &[GUARDED_MIGRATION];

static COPY_THEN_FENCE: EmbeddedMigration = EmbeddedMigration {
    version: 421,
    filename: "421_copy_probe.sql",
    checksum: "7ec5f3b7cf557fcee6903676bc89a7ff89ed0c1100e44775e5df1a01d3c38689",
    sql: "CREATE TABLE copy_probe (id integer);\n",
};
static DESTRUCTIVE_AFTER_COPY: EmbeddedMigration = EmbeddedMigration {
    version: 422,
    filename: "422_destructive_probe.sql",
    checksum: "c5824af6e3aa4151609e330dca97948d7ba3a22293248883d5fc4d335165638e",
    sql: "-- gobby:destructive\nCREATE TABLE drop_probe (id integer);\n",
};
static COPY_THEN_DESTRUCTIVE: &[EmbeddedMigration] = &[COPY_THEN_FENCE, DESTRUCTIVE_AFTER_COPY];

static DATABASE_TEST_LOCK: Mutex<()> = Mutex::new(());

const PRIOR_BASELINE_VERSION: i32 = 419;
const PRIOR_BASELINE_CHECKSUM: &str =
    "a361cb10d591e82aeb0e1ce04eb09e64e468ef571dcd3ae492eccb16cbb4ce81";

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

fn scratch_database() -> anyhow::Result<Option<(ScratchDatabase, Client)>> {
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
    // project_checkouts is read-only for gcode: SELECT plus the UPDATE policy
    // that authorizes SELECT ... FOR SHARE under FORCE RLS while denying writes.
    let checkout_policies = actual
        .remove("project_checkouts")
        .expect("project_checkouts must carry the scoped gcode read/update policies");
    let checkout_operations: Vec<(String, String)> = checkout_policies
        .iter()
        .map(|(name, command, _, _)| (name.clone(), command.clone()))
        .collect();
    assert_eq!(
        checkout_operations,
        [
            ("gobby_gcode_project_read".to_owned(), "SELECT".to_owned()),
            ("gobby_gcode_project_update".to_owned(), "UPDATE".to_owned()),
        ]
    );
    for (name, _, using_expression, check_expression) in checkout_policies {
        assert!(using_expression.contains("current_project_id"));
        assert!(using_expression.contains("current_machine_id"));
        if name == "gobby_gcode_project_update" {
            assert_eq!(check_expression, "false");
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
    let Some((_database, mut client)) = scratch_database()? else {
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
    let Some((_database, mut client)) = scratch_database()? else {
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
    let Some((_database, mut client)) = scratch_database()? else {
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
fn interactive_principal_binds_only_registered_worktree_overlays() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = scratch_database()? else {
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
        "INSERT INTO projects(id, name) VALUES ($1, 'parent'), ($2, 'unrelated')",
        &[&project_id, &unrelated_project_id],
    )?;
    client.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) \
         VALUES ($1, $2, '/tmp/gobby-ix-parent'), ($1, $3, '/tmp/gobby-ix-unrelated')",
        &[&machine_id, &project_id, &unrelated_project_id],
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
    let Some((database, mut client)) = scratch_database()? else {
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
        "INSERT INTO projects(id, name) VALUES ($1, 'parent'), ($2, 'unrelated')",
        &[&project_id, &unrelated_project_id],
    )?;
    client.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) \
         VALUES ($1, $2, '/tmp/gobby-parent'), ($1, $3, '/tmp/unrelated')",
        &[&machine_id, &project_id, &unrelated_project_id],
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
    let Some((_database, mut client)) = scratch_database()? else {
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
        "INSERT INTO projects(id, name) VALUES ($1, 'tool-binding')",
        &[&project_id],
    )?;
    client.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) \
         VALUES ($1, $2, '/tmp/gobby-tool-binding')",
        &[&machine_id, &project_id],
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
    let Some((_database, mut client)) = scratch_database()? else {
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
    let Some((_database, mut client)) = scratch_database()? else {
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
        "UPDATE schema_migrations SET filename = 'unexpected@420', checksum = $1 WHERE version = $2",
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
fn prior_canonical_baseline_receipt_is_accepted() -> anyhow::Result<()> {
    assert_eq!(BASELINE_VERSION, PRIOR_BASELINE_VERSION + 1);
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = scratch_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;
    client.execute(
        "UPDATE schema_migrations SET version = $1, filename = $2, checksum = $3 \
         WHERE version = $4",
        &[
            &PRIOR_BASELINE_VERSION,
            &format!("baseline@{PRIOR_BASELINE_VERSION}"),
            &PRIOR_BASELINE_CHECKSUM,
            &BASELINE_VERSION,
        ],
    )?;

    let report = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(!report.baseline_applied);
    assert_eq!(report.migrations_applied, 0);
    SchemaRunner::new(&mut client, "public")?.verify()?;
    Ok(())
}

#[test]
fn lock_and_recovery_tests_repair_an_invalid_concurrent_index() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = scratch_database()? else {
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
    let Some((database, mut lock_client)) = scratch_database()? else {
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
    let Some((database, mut client)) = scratch_database()? else {
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
    let Some((_database, mut client)) = scratch_database()? else {
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
fn migrations_directory_exists_and_registry_is_empty_after_flatten() {
    let migrations_dir =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("assets/schema/migrations");
    assert!(
        migrations_dir.is_dir(),
        "crates/gcore/assets/schema/migrations must exist for versions after baseline@420"
    );
    assert!(MIGRATIONS.is_empty());
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
    let Some((_database, mut client)) = scratch_database()? else {
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
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 421 AND filename = $1 AND checksum = $2",
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
    let Some((_database, mut client)) = scratch_database()? else {
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
    let Some((_database, mut client)) = scratch_database()? else {
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
fn guarded_nondestructive_migration_applies_on_fresh() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = scratch_database()? else {
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
    Ok(())
}

#[test]
fn code_inheritance_has_gcode_project_policies() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = scratch_database()? else {
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
        "INSERT INTO projects(id, name) VALUES ($1, 'parent'), ($2, 'other')",
        &[&project_id, &other_project_id],
    )?;
    client.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) \
         VALUES ($1, $2, '/tmp/gobby-inheritance'), ($1, $3, '/tmp/other')",
        &[&machine_id, &project_id, &other_project_id],
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
    let Some((_database, mut client)) = scratch_database()? else {
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
fn code_inheritance_adoption_rejects_pre_inheritance_and_keeps_existing() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = scratch_database()? else {
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
    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("flattened baseline adoption must reject a partial schema");
    assert!(matches!(
        error,
        SchemaError::Unsupported(message)
            if message.starts_with("cannot adopt code_inheritance; missing required columns:")
    ));

    let Some((_database2, mut existing)) = scratch_database()? else {
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
fn render_gives_each_non_public_hub_its_own_agent_auth_schema() {
    let sql = "CREATE SCHEMA IF NOT EXISTS gobby_agent_auth;\n\
               CREATE OR REPLACE FUNCTION gobby_agent_auth.heartbeat_daemon() \
               SET search_path = gobby_agent_auth, pg_temp AS $$ \
               SELECT 1 FROM public.machines $$;";
    assert_eq!(auth_schema_for("public"), "gobby_agent_auth");
    assert_eq!(render_sql_for_schema(sql, "public"), sql);

    let rendered = render_sql_for_schema(sql, "gobby_test_1_2_w_abc");
    assert_eq!(
        auth_schema_for("gobby_test_1_2_w_abc"),
        "gobby_test_1_2_w_abc_agent_auth"
    );
    assert!(!rendered.contains("gobby_agent_auth"));
    assert!(!rendered.contains("public."));
    assert!(rendered.contains("CREATE SCHEMA IF NOT EXISTS gobby_test_1_2_w_abc_agent_auth;"));
    assert!(rendered.contains("gobby_test_1_2_w_abc_agent_auth.heartbeat_daemon()"));
    assert!(rendered.contains("search_path = gobby_test_1_2_w_abc_agent_auth, pg_temp"));
    assert!(rendered.contains("FROM gobby_test_1_2_w_abc.machines"));
}
