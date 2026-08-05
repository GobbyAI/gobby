use std::env;

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
