use std::env;

use anyhow::{Context, Result};
use assert_cmd::Command;
use gobby_core::postgres::connect_readwrite;
use gobby_core::schema::{SchemaIdentityContract, SchemaRunner};

const DATABASE_URL_ENV: &str = "GOBBY_TEST_POSTGRES_URL";

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

fn scoped_database_url(database_url: &str, schema: &str) -> String {
    let separator = if database_url.contains('?') { '&' } else { '?' };
    format!("{database_url}{separator}options=-csearch_path%3D{schema}")
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

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", &scratch.name])
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
    let scoped_url = scoped_database_url(&database_url, &scratch.name);

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply"])
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
    let authority = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_sweep_authority_{process_id}"),
    };
    let stale = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gobby_test_0_{process_id}_stale_deadbeef"),
    };
    let live = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gobby_test_0_{process_id}_live_cafebabe"),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    SchemaRunner::new(&mut admin, &authority.name)?.apply()?;
    admin.batch_execute(&format!(
        "CREATE SCHEMA \"{}\"; CREATE SCHEMA \"{}\"",
        stale.name, live.name,
    ))?;
    admin.query_one("SELECT pg_advisory_lock(hashtext($1))", &[&live.name])?;

    let scoped_url = scoped_database_url(&database_url, &authority.name);
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "sweep-test-schemas", "--age-hours", "1"])
        .env("GOBBY_DATABASE_URL", scoped_url)
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
fn sweep_rejects_database_schema_identity_mismatch() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_sweep_mismatch_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    SchemaRunner::new(&mut admin, &scratch.name)?.apply()?;
    let embedded_version = SchemaIdentityContract::embedded().latest_version;
    admin.execute(
        &format!(
            "DELETE FROM \"{}\".schema_migrations WHERE version = $1",
            scratch.name
        ),
        &[&embedded_version],
    )?;

    let scoped_url = scoped_database_url(&database_url, &scratch.name);
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "sweep-test-schemas", "--age-hours", "1"])
        .env("GOBBY_DATABASE_URL", scoped_url)
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(
        stderr.contains(&format!(
            "binary-embedded schema identity v{embedded_version} does not match database schema v{}",
            embedded_version - 1
        )),
        "{stderr}"
    );
    Ok(())
}

#[test]
fn plan_reports_fresh_schema_without_creating_it() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_plan_fresh_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "DROP SCHEMA IF EXISTS \"{}\" CASCADE",
        scratch.name
    ))?;

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "plan", "--schema", &scratch.name])
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(
        output.status.success(),
        "gdaemon plan failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout)?;
    assert!(stdout.contains("baseline_pending=true"), "{stdout}");

    let exists: bool = admin
        .query_one(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)",
            &[&scratch.name],
        )?
        .get(0);
    assert!(!exists, "plan must not create the schema it reports on");
    Ok(())
}

#[test]
fn plan_after_apply_reports_no_pending() -> Result<()> {
    let Ok(database_url) = env::var(DATABASE_URL_ENV) else {
        eprintln!("skipped: {DATABASE_URL_ENV} is not set");
        return Ok(());
    };
    let scratch = ScratchSchema {
        database_url: database_url.clone(),
        name: format!("gdaemon_plan_applied_{}", std::process::id()),
    };
    let mut admin = connect_readwrite(&database_url).context("connect to test PostgreSQL")?;
    admin.batch_execute(&format!(
        "DROP SCHEMA IF EXISTS \"{}\" CASCADE",
        scratch.name
    ))?;

    let applied = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", &scratch.name])
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(
        applied.status.success(),
        "gdaemon apply failed: {}",
        String::from_utf8_lossy(&applied.stderr)
    );

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "plan", "--schema", &scratch.name])
        .env("GOBBY_DATABASE_URL", &database_url)
        .output()?;
    assert!(
        output.status.success(),
        "gdaemon plan failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout)?;
    assert!(stdout.contains("pending_migrations=0"), "{stdout}");
    assert!(stdout.contains("baseline_pending=false"), "{stdout}");
    Ok(())
}
