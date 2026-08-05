#![cfg(feature = "postgres")]

use std::env;
use std::sync::Mutex;

use gobby_core::schema::{SchemaRunner, catalog_manifest, render_catalog_manifest};
use postgres::{Client, Config, NoTls};
use uuid::Uuid;

static DATABASE_TEST_LOCK: Mutex<()> = Mutex::new(());

struct ScratchDatabase {
    admin: Client,
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
        Ok((Self { admin, name }, client))
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
    assert_eq!(first.migrations_applied, 0);

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
        "INSERT INTO schema_migrations(version, filename, checksum) VALUES (376, '376_future.sql', $1)",
        &[&"f".repeat(64)],
    )?;

    let error = SchemaRunner::new(&mut client, "public")?
        .apply()
        .expect_err("older runner must reject newer database");
    assert!(error.to_string().contains("newer than this runner"));
    Ok(())
}
