use std::env;
use std::path::PathBuf;

use anyhow::{Context, Result};
use assert_cmd::Command;
use gobby_core::postgres::connect_readwrite;
use gobby_core::schema::SchemaRunner;

const DATABASE_URL_ENV: &str = "GOBBY_TEST_POSTGRES_URL";
const EXPECTED_IDENTITY_ENV: &str = "GOBBY_EXPECTED_SCHEMA_IDENTITY";

struct ScratchSchema {
    database_url: String,
    name: String,
}

impl Drop for ScratchSchema {
    fn drop(&mut self) {
        if let Ok(mut client) = connect_readwrite(&self.database_url) {
            let _ =
                client.batch_execute(&format!("DROP SCHEMA IF EXISTS \"{}\" CASCADE", self.name));
        }
    }
}

struct ScratchHome {
    path: PathBuf,
}

impl Drop for ScratchHome {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

impl ScratchHome {
    fn create() -> Result<Self> {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!(
            "../../target/gdaemon-schema-cli-home-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).context("create per-process schema CLI GOBBY_HOME")?;
        Ok(Self { path })
    }
}

#[test]
fn apply_builds_verified_baseline_in_named_schema() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_cli_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "DROP SCHEMA IF EXISTS \"{}\" CASCADE",
        scratch.name
    ))?;

    let identity = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(identity.status.success());
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", &scratch.name])
        .env(EXPECTED_IDENTITY_ENV, String::from_utf8(identity.stdout)?)
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(
        output.status.success(),
        "gdaemon apply failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let report = SchemaRunner::new(&mut admin, &scratch.name)?.verify()?;
    assert!(report.checked_receipts > 0);
    assert!(report.checked_seed_rows > 0);
    assert!(report.checked_catalog_objects > 0);
    Ok(())
}

#[test]
fn apply_uses_connection_current_schema_by_default() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_current_schema_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "DROP SCHEMA IF EXISTS \"{}\" CASCADE; CREATE SCHEMA \"{}\"",
        scratch.name, scratch.name
    ))?;
    let separator = if database_url.contains('?') { '&' } else { '?' };
    let scoped_url = format!(
        "{database_url}{separator}options=-csearch_path%3D{}",
        scratch.name
    );

    let identity = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(identity.status.success());
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply"])
        .env(EXPECTED_IDENTITY_ENV, String::from_utf8(identity.stdout)?)
        .env("GOBBY_DATABASE_URL", scoped_url)
        .output()?;
    assert!(
        output.status.success(),
        "gdaemon apply failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let report = SchemaRunner::new(&mut admin, &scratch.name)?.verify()?;
    assert!(report.checked_receipts > 0);
    assert!(report.checked_seed_rows > 0);
    assert!(report.checked_catalog_objects > 0);
    Ok(())
}

#[test]
fn sweep_drops_only_aged_unlocked_test_schemas() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let process_id = std::process::id();
    let stale = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gobby_test_0_{process_id}_stale_deadbeef"),
    };
    let live = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gobby_test_0_{process_id}_live_cafebabe"),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "CREATE SCHEMA \"{}\"; CREATE SCHEMA \"{}\"",
        stale.name, live.name,
    ))?;
    admin.query_one("SELECT pg_advisory_lock(hashtext($1))", &[&live.name])?;

    let identity = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(identity.status.success());
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "sweep-test-schemas", "--age-hours", "1"])
        .env(EXPECTED_IDENTITY_ENV, String::from_utf8(identity.stdout)?)
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;

    assert!(
        output.status.success(),
        "gdaemon sweep failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stale_exists: bool = admin
        .query_one(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = $1)",
            &[&stale.name],
        )?
        .get(0);
    let live_exists: bool = admin
        .query_one(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = $1)",
            &[&live.name],
        )?
        .get(0);
    admin.query_one("SELECT pg_advisory_unlock(hashtext($1))", &[&live.name])?;

    assert!(!stale_exists);
    assert!(live_exists);
    Ok(())
}

#[test]
fn destructive_apply_refuses_without_open_maintenance_epoch() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let identity = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(identity.status.success());
    let output = Command::cargo_bin("gdaemon")?
        .args([
            "schema",
            "apply",
            "--destructive",
            "--schema",
            "gdaemon_epoch_refuse",
        ])
        .env(EXPECTED_IDENTITY_ENV, String::from_utf8(identity.stdout)?)
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("gobby hub-maintenance run schema-apply"),
        "refusal must name the orchestrated apply: {stderr}"
    );
    Ok(())
}

#[test]
fn destructive_apply_succeeds_with_epoch_bound_dsn_and_verified_backup() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_epoch_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "DROP SCHEMA IF EXISTS \"{}\" CASCADE; CREATE SCHEMA \"{}\"",
        scratch.name, scratch.name
    ))?;

    let identity = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(identity.status.success());
    let identity_json = String::from_utf8(identity.stdout)?;

    let first = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", &scratch.name])
        .env(EXPECTED_IDENTITY_ENV, &identity_json)
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(
        first.status.success(),
        "initial apply failed: {}",
        String::from_utf8_lossy(&first.stderr)
    );

    admin.batch_execute(&format!(
        "SET search_path TO \"{}\"; \
         INSERT INTO maintenance_epochs (id, campaign, opened_by, scope_note) \
         VALUES ('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1', 'schema-apply', 'test', 'epoch fence')",
        scratch.name
    ))?;

    let row = admin.query_one(
        "SELECT (pg_control_system()).system_identifier::text, current_database(), oid, \
                COALESCE((SELECT MAX(version) FROM schema_migrations), 0), \
                to_char(timezone('UTC', now()), 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') \
         FROM pg_database WHERE datname = current_database()",
        &[],
    )?;
    let mut manifest: serde_json::Value = serde_json::from_str(include_str!(
        "../../gcore/tests/fixtures/hub_backup_manifest/v3_roundtrip.json"
    ))?;
    manifest["source_identity"]["pg_system_identifier"] =
        serde_json::json!(row.get::<_, String>(0));
    manifest["source_identity"]["database_name"] = serde_json::json!(row.get::<_, String>(1));
    manifest["source_identity"]["database_oid"] = serde_json::json!(row.get::<_, u32>(2));
    manifest["backup_starting_head"] = serde_json::json!(row.get::<_, i32>(3));
    manifest["created_at"] = serde_json::json!(row.get::<_, String>(4));
    manifest["artifacts"][0]["sha256"] =
        serde_json::json!("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    manifest["artifacts"][0]["size_bytes"] = serde_json::json!(0);

    let home = ScratchHome::create()?;
    let backup_dir = home.path.join("backups/hub/epoch-fence");
    std::fs::create_dir_all(backup_dir.join("postgres"))?;
    std::fs::write(backup_dir.join("postgres/fixture.dump"), [])?;
    std::fs::write(
        backup_dir.join("manifest.json"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;

    let separator = if database_url.contains('?') { '&' } else { '?' };
    let bound_url = format!(
        "{database_url}{separator}options=-csearch_path%3D{}%20-cgobby.maintenance_epoch%3Daaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1",
        scratch.name
    );
    let output = Command::cargo_bin("gdaemon")?
        .args([
            "schema",
            "apply",
            "--destructive",
            "--schema",
            &scratch.name,
        ])
        .env(EXPECTED_IDENTITY_ENV, identity_json)
        .env("GOBBY_DATABASE_URL", bound_url)
        .env("GOBBY_HOME", &home.path)
        .output()?;
    assert!(
        output.status.success(),
        "epoch-bound destructive apply failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

#[test]
fn destructive_apply_refuses_after_epoch_is_released() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_epoch_released_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "DROP SCHEMA IF EXISTS \"{}\" CASCADE; CREATE SCHEMA \"{}\"",
        scratch.name, scratch.name
    ))?;

    let identity = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(identity.status.success());
    let identity_json = String::from_utf8(identity.stdout)?;

    let first = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", &scratch.name])
        .env(EXPECTED_IDENTITY_ENV, &identity_json)
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(
        first.status.success(),
        "initial apply failed: {}",
        String::from_utf8_lossy(&first.stderr)
    );

    admin.batch_execute(&format!(
        "SET search_path TO \"{}\"; \
         INSERT INTO maintenance_epochs \
            (id, campaign, opened_by, scope_note, released_at, released_by_command) \
         VALUES (\
            'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee2', \
            'schema-apply', 'test', 'released fence', NOW(), 'test-release')",
        scratch.name
    ))?;

    let separator = if database_url.contains('?') { '&' } else { '?' };
    let bound_url = format!(
        "{database_url}{separator}options=-csearch_path%3D{}%20-cgobby.maintenance_epoch%3Daaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee2",
        scratch.name
    );
    let output = Command::cargo_bin("gdaemon")?
        .args([
            "schema",
            "apply",
            "--destructive",
            "--schema",
            &scratch.name,
        ])
        .env(EXPECTED_IDENTITY_ENV, identity_json)
        .env("GOBBY_DATABASE_URL", bound_url)
        .output()?;
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("is not an open maintenance epoch"),
        "released epoch must fail closed: {stderr}"
    );
    assert!(
        stderr.contains("gobby hub-maintenance run schema-apply"),
        "refusal must name the orchestrated apply: {stderr}"
    );
    Ok(())
}
