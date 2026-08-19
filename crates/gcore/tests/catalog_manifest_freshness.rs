#![cfg(feature = "postgres")]

use std::env;
use std::sync::Mutex;

use gobby_core::schema::{
    CATALOG_MANIFEST_JSON, SchemaRunner, catalog_manifest, render_catalog_manifest,
};
use postgres::{Client, Config, NoTls};
use uuid::Uuid;

static DATABASE_TEST_LOCK: Mutex<()> = Mutex::new(());

struct ScratchDatabase {
    admin: Client,
    config: Config,
    name: String,
}

impl ScratchDatabase {
    fn create(database_url: &str) -> anyhow::Result<(Self, Client)> {
        let config: Config = database_url.parse()?;
        let mut admin = config.connect(NoTls)?;
        let name = format!("gcore_schema_{}", Uuid::new_v4().simple());
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

    fn connect_with_epoch(&self, epoch: Uuid) -> Result<Client, postgres::Error> {
        let mut config = self.config.clone();
        config.options(&format!("-c gobby.maintenance_epoch={epoch}"));
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

fn test_database() -> anyhow::Result<Option<(ScratchDatabase, Client)>> {
    let Ok(database_url) = env::var("GOBBY_SCHEMA_TEST_DATABASE_URL") else {
        eprintln!("GOBBY_SCHEMA_TEST_DATABASE_URL is unset; skipping PostgreSQL schema test");
        return Ok(None);
    };
    ScratchDatabase::create(&database_url).map(Some)
}

#[test]
fn embedded_runner_applies_fresh_and_idempotently() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let first = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(first.baseline_applied);
    assert_eq!(first.migrations_applied, 20);

    let second = SchemaRunner::new(&mut client, "public")?.apply()?;
    assert!(!second.baseline_applied);
    assert_eq!(second.migrations_applied, 0);
    Ok(())
}

#[test]
fn named_schema_apply_and_verify_are_isolated() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };

    let mut runner = SchemaRunner::new(&mut client, "tenant_schema")?;
    let first = runner.apply()?;
    let second = runner.apply()?;
    let verified = runner.verify()?;
    assert!(first.baseline_applied);
    assert!(!second.baseline_applied);
    assert!(verified.checked_catalog_objects > 0);

    let tenant_tasks: bool = client
        .query_one("SELECT to_regclass('tenant_schema.tasks') IS NOT NULL", &[])?
        .get(0);
    let public_tasks: bool = client
        .query_one("SELECT to_regclass('public.tasks') IS NOT NULL", &[])?
        .get(0);
    assert!(tenant_tasks);
    assert!(!public_tasks);
    Ok(())
}

#[test]
fn login_trigger_allows_connections_after_its_relation_is_dropped() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    let trigger_exists: bool = client
        .query_one(
            "SELECT EXISTS (
                SELECT 1 FROM pg_event_trigger
                WHERE evtname LIKE 'gobby_maintenance_epoch_login_%'
            )",
            &[],
        )?
        .get(0);
    assert!(
        trigger_exists,
        "public baseline must install the login trigger"
    );

    client.batch_execute("DROP TABLE public.maintenance_epochs CASCADE")?;
    let trigger_survives_drop: bool = client
        .query_one(
            "SELECT EXISTS (
                SELECT 1 FROM pg_event_trigger
                WHERE evtname LIKE 'gobby_maintenance_epoch_login_%'
            )",
            &[],
        )?
        .get(0);
    assert!(
        trigger_survives_drop,
        "relation drop must leave the login trigger installed"
    );
    let _connection = database.connect()?;
    Ok(())
}

#[test]
fn login_trigger_preserves_active_epoch_fencing() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    let epoch = Uuid::new_v4();
    client.execute(
        "INSERT INTO public.maintenance_epochs (
            id, campaign, opened_by, scope_note
        ) VALUES ($1, 'schema-apply', 'schema-contract-test', 'login fence regression')",
        &[&epoch],
    )?;

    let error = database
        .connect()
        .err()
        .ok_or_else(|| anyhow::anyhow!("active epoch must reject an unannotated login"))?;
    let database_error = error
        .as_db_error()
        .ok_or_else(|| anyhow::anyhow!("expected PostgreSQL login rejection: {error}"))?;
    assert_eq!(database_error.code().code(), "55000");
    assert!(
        database_error
            .message()
            .contains("Gobby hub maintenance is active")
    );

    let _annotated_connection = database.connect_with_epoch(epoch)?;
    Ok(())
}

#[test]
fn named_schema_applies_never_install_database_login_triggers() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((database, mut client)) = test_database()? else {
        return Ok(());
    };
    for schema in ["login_trigger_schema_a", "login_trigger_schema_b"] {
        SchemaRunner::new(&mut client, schema)?.apply()?;
    }
    let trigger_count: i64 = client
        .query_one(
            "SELECT count(*) FROM pg_event_trigger
             WHERE evtname LIKE 'gobby_maintenance_epoch_login_%'",
            &[],
        )?
        .get(0);
    assert_eq!(trigger_count, 0);

    client.batch_execute("DROP SCHEMA login_trigger_schema_a CASCADE")?;
    let _first_connection = database.connect()?;
    client.batch_execute("DROP SCHEMA login_trigger_schema_b CASCADE")?;
    let _second_connection = database.connect()?;
    Ok(())
}

#[test]
fn catalog_identity_ignores_column_ordinals() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    client.batch_execute(
        "CREATE SCHEMA first_order;
         CREATE TABLE first_order.sample (id uuid NOT NULL, detail text);
         CREATE TABLE first_order.gwiki_external_projection (id uuid PRIMARY KEY);
         CREATE FUNCTION first_order.stable_label() RETURNS text
             LANGUAGE sql IMMUTABLE AS $$
             -- Function-body comments are not executable schema semantics.
             SELECT 'stable'::text;
             $$;
         CREATE SCHEMA last_order;
         CREATE TABLE last_order.sample (detail text, id uuid NOT NULL);
         CREATE FUNCTION last_order.stable_label() RETURNS text
             LANGUAGE sql IMMUTABLE AS $$
             SELECT 'stable'::text;
             $$;
         CREATE EXTENSION pgcrypto WITH SCHEMA first_order;",
    )?;

    let first = catalog_manifest(&mut client, "first_order")?;
    let last = catalog_manifest(&mut client, "last_order")?;
    assert_eq!(first, last);
    assert!(
        first
            .constraints
            .iter()
            .all(|entry| !entry.name.ends_with("_not_null"))
    );
    Ok(())
}

#[test]
fn verify_accepts_runtime_mutation_of_seed_fields() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    client.batch_execute(
        "UPDATE projects
            SET repo_path = '/installed/personal'
          WHERE id = '00000000-0000-0000-0000-000000060887';
         UPDATE sessions
            SET status = 'ended', message_count = 9
          WHERE id = '00000000-0000-0000-0000-000000000001';",
    )?;

    SchemaRunner::new(&mut client, "public")?.verify()?;
    Ok(())
}

#[test]
fn catalog_manifest_is_fresh_for_embedded_assets() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;

    let generated = render_catalog_manifest(&catalog_manifest(&mut client, "public")?)?;
    let checked_in = include_str!("../assets/schema/catalog.manifest.json");
    if env::var_os("UPDATE_GCORE_SCHEMA_MANIFEST").is_some() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("assets/schema/catalog.manifest.json");
        std::fs::write(path, generated)?;
        return Ok(());
    }
    assert_eq!(generated, checked_in, "catalog manifest is stale");
    Ok(())
}

#[test]
fn baseline_enforces_workspace_session_machine_ownership() -> anyhow::Result<()> {
    let manifest: serde_json::Value = serde_json::from_str(CATALOG_MANIFEST_JSON)?;
    let entries = |kind: &str| {
        manifest[kind]
            .as_array()
            .expect("catalog entry kind must be an array")
    };
    let definition = |kind: &str, name: &str| {
        entries(kind)
            .iter()
            .find(|entry| entry["name"] == name)
            .unwrap_or_else(|| panic!("missing {kind} entry {name}"))["definition"]
            .as_str()
            .expect("catalog definition must be a string")
    };

    assert_eq!(
        definition("columns", "sessions.machine_id"),
        "uuid|uuid|NO||NEVER"
    );
    assert_eq!(
        definition("constraints", "sessions.sessions_id_machine_id_key"),
        "UNIQUE (id, machine_id)"
    );
    assert_eq!(
        definition(
            "constraints",
            "worktrees.worktrees_agent_session_id_machine_id_fkey"
        ),
        "FOREIGN KEY (agent_session_id, machine_id) REFERENCES sessions(id, machine_id) \
ON DELETE SET NULL (agent_session_id) DEFERRABLE"
    );
    assert_eq!(
        definition(
            "constraints",
            "clones.clones_agent_session_id_machine_id_fkey"
        ),
        "FOREIGN KEY (agent_session_id, machine_id) REFERENCES sessions(id, machine_id) \
ON DELETE SET NULL (agent_session_id) DEFERRABLE"
    );
    Ok(())
}

#[test]
fn verify_contract_detects_workspace_constraint_drift() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;

    for mutation in [
        "ALTER TABLE sessions RENAME CONSTRAINT sessions_id_machine_id_key \
TO sessions_id_machine_id_key_renamed",
        "ALTER TABLE worktrees DROP CONSTRAINT worktrees_agent_session_id_machine_id_fkey",
        "ALTER TABLE clones DROP CONSTRAINT clones_agent_session_id_machine_id_fkey; \
ALTER TABLE clones ADD CONSTRAINT clones_agent_session_id_machine_id_fkey \
FOREIGN KEY (agent_session_id, machine_id) REFERENCES sessions(id, machine_id) \
ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE",
    ] {
        client.batch_execute("BEGIN")?;
        client.batch_execute(mutation)?;
        let error = SchemaRunner::new(&mut client, "public")?
            .verify()
            .expect_err("workspace constraint drift must fail verification");
        assert!(error.to_string().contains("catalog"), "{mutation}: {error}");
        client.batch_execute("ROLLBACK")?;
    }
    Ok(())
}

#[test]
fn verify_contract_detects_catalog_seed_and_bookkeeping_drift() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    SchemaRunner::new(&mut client, "public")?.verify()?;

    for mutation in [
        "DROP INDEX idx_projects_name",
        "ALTER TABLE projects DROP CONSTRAINT projects_pkey CASCADE",
        "ALTER TABLE projects DROP COLUMN github_url",
    ] {
        client.batch_execute("BEGIN")?;
        client.batch_execute(mutation)?;
        let error = SchemaRunner::new(&mut client, "public")?
            .verify()
            .expect_err("catalog drift must fail verification");
        assert!(error.to_string().contains("catalog"), "{mutation}: {error}");
        client.batch_execute("ROLLBACK")?;
    }

    client.batch_execute("BEGIN")?;
    client.execute(
        "DELETE FROM projects WHERE id = $1",
        &[&Uuid::parse_str("00000000-0000-0000-0000-000000000000")?],
    )?;
    let seed_error = SchemaRunner::new(&mut client, "public")?
        .verify()
        .expect_err("seed drift must fail verification");
    assert!(seed_error.to_string().contains("seed"));
    client.batch_execute("ROLLBACK")?;

    client.batch_execute("BEGIN")?;
    client.execute(
        "UPDATE schema_migrations SET checksum = 'bad' WHERE version = 375",
        &[],
    )?;
    let receipt_error = SchemaRunner::new(&mut client, "public")?
        .verify()
        .expect_err("bookkeeping drift must fail verification");
    assert!(receipt_error.to_string().contains("receipt"));
    client.batch_execute("ROLLBACK")?;
    Ok(())
}

#[test]
fn guard_test_rejects_a_database_newer_than_the_embedded_runner() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    client.execute(
        "INSERT INTO schema_migrations(version, filename, checksum) VALUES (396, '396_future.sql', $1)",
        &[&"f".repeat(64)],
    )?;

    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("older runner must reject newer database");
    assert!(error.to_string().contains("newer than this runner"));
    Ok(())
}

#[test]
fn task_delete_foreign_key_lookup_uses_the_dispatch_task_index() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    client.batch_execute("SET enable_seqscan = off")?;

    let plan = client
        .query(
            "EXPLAIN (COSTS OFF) SELECT 1 FROM gh_triage_build_dispatches WHERE task_id = $1",
            &[&Uuid::nil()],
        )?
        .into_iter()
        .map(|row| row.get::<_, String>(0))
        .collect::<Vec<_>>()
        .join("\n");

    assert!(
        plan.contains("idx_gh_triage_build_dispatches_task_id"),
        "unexpected query plan:\n{plan}"
    );
    Ok(())
}

#[test]
fn baseline_supports_machine_owned_attachments_and_prune_rows() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    SchemaRunner::new(&mut client, "public")?.apply()?;
    client.batch_execute(
        "
        INSERT INTO users (id, email, name, password_hash)
        VALUES ('99999999-9999-4999-8999-999999999999', 'schema@test.local', 'schema-test', 'x');
        INSERT INTO machines (id, hostname, owner_user_id)
        VALUES (
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'migration-test',
            '99999999-9999-4999-8999-999999999999'
        );
        INSERT INTO projects (id, name)
        VALUES ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'migration-test');
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (
            'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
            'migration-test',
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'test',
            'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        );
        INSERT INTO chat_attachments (
            id, machine_id, project_id, target_session_id, filename, mime_type, size_bytes,
            local_path
        ) VALUES (
            'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
            'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
            'chat.txt', 'text/plain', 4, '/tmp/chat.txt'
        );
        INSERT INTO comms_channels (id, channel_type, name)
        VALUES ('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', 'test', 'migration-test');
        INSERT INTO comms_messages (id, channel_id, direction, content, session_id)
        VALUES (
            'ffffffff-ffff-4fff-8fff-ffffffffffff',
            'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
            'inbound', 'test', 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        );
        INSERT INTO comms_attachments (id, machine_id, message_id, filename, local_path)
        VALUES (
            '11111111-1111-4111-8111-111111111111',
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'ffffffff-ffff-4fff-8fff-ffffffffffff',
            'comms.txt', '/tmp/comms.txt'
        );
        ",
    )?;

    for table in ["chat_attachments", "comms_attachments"] {
        let machine_id: String = client
            .query_one(&format!("SELECT machine_id::text FROM {table}"), &[])?
            .get(0);
        assert_eq!(machine_id, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    }
    client.batch_execute(
        "
        INSERT INTO code_index_prune_dirty_projects (
            machine_id, project_id, root_path, reason
        ) VALUES
            (
                'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
                '/machine-a/project', 'test'
            );
        INSERT INTO machines (id, hostname, owner_user_id)
        VALUES (
            '22222222-2222-4222-8222-222222222222',
            'migration-test-2',
            '99999999-9999-4999-8999-999999999999'
        );
        INSERT INTO code_index_prune_dirty_projects (
            machine_id, project_id, root_path, reason
        ) VALUES
            (
                '22222222-2222-4222-8222-222222222222',
                'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
                '/machine-b/project', 'test'
            );
        ",
    )?;
    let count: i64 = client
        .query_one("SELECT count(*) FROM code_index_prune_dirty_projects", &[])?
        .get(0);
    assert_eq!(count, 2);
    Ok(())
}
