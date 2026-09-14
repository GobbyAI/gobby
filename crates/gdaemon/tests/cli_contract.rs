use assert_cmd::Command;
use gobby_core::schema::schema_identity;

const DATABASE_URL_ENV: &str = "GOBBY_DATABASE_URL";
const SECRET_DSN: &str = "postgresql://schema_user:do-not-leak@127.0.0.1:1/gobby";

#[test]
fn schema_help_exposes_test_schema_sweep() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "--help"])
        .output()?;
    let stdout = String::from_utf8(output.stdout.clone())?;

    assert!(output.status.success(), "{output:?}");
    assert!(stdout.contains("sweep-test-schemas"), "{stdout}");
    Ok(())
}

#[test]
fn version_json_reports_exact_schema_identity_contract() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;

    assert!(output.status.success(), "{output:?}");
    let identity: serde_json::Value = serde_json::from_slice(&output.stdout)?;
    let fields = identity
        .as_object()
        .expect("schema identity must be a JSON object");
    assert_eq!(
        fields.keys().map(String::as_str).collect::<Vec<_>>(),
        [
            "assets_root_hash",
            "baseline_checksum",
            "baseline_version",
            "latest_checksum",
            "latest_version",
            "runner_protocol",
        ]
    );
    // The value pin lives once, in gcore's schema_contract.rs. What this test owns is
    // the CLI mapping: every embedded field has to reach the published JSON under its
    // contract name, and `schema_identity` renames all six on the way out. Restating
    // the checksums here made this a sixth place a schema bump had to be remembered,
    // and it was the one missed at 432.
    let embedded = schema_identity();
    assert_eq!(
        identity["runner_protocol"],
        embedded.runner_protocol_version
    );
    assert_eq!(identity["baseline_version"], embedded.baseline.version);
    assert_eq!(identity["baseline_checksum"], embedded.baseline.checksum);
    assert_eq!(identity["latest_version"], embedded.latest_asset.version);
    assert_eq!(identity["latest_checksum"], embedded.latest_asset.checksum);
    assert_eq!(identity["assets_root_hash"], embedded.root_hash);
    // One literal stays as the human tripwire, deliberately: a bare version number is
    // something a reviewer can verify at a glance, which was never true of a checksum.
    assert_eq!(identity["latest_version"], 437);
    assert_eq!(
        identity["assets_root_hash"].as_str().map(str::len),
        Some(64)
    );
    Ok(())
}

#[test]
fn apply_ignores_checkout_identity_environment() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply"])
        .env(
            "GOBBY_EXPECTED_SCHEMA_IDENTITY",
            r#"{"latest_version":999}"#,
        )
        .env(DATABASE_URL_ENV, SECRET_DSN)
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(stderr.contains("failed to connect"));
    assert!(!stderr.contains("expected schema identity"));
    assert!(!stderr.contains("schema_user"));
    assert!(!stderr.contains("do-not-leak"));
    Ok(())
}

#[test]
fn apply_rejects_malicious_schema_before_connecting() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", "bad\";drop schema public;--"])
        .env(DATABASE_URL_ENV, SECRET_DSN)
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(stderr.contains("invalid PostgreSQL schema name"));
    assert!(!stderr.contains("failed to connect"));
    assert!(!stderr.contains("do-not-leak"));
    Ok(())
}

#[test]
fn apply_has_no_dsn_argument() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args([
            "schema",
            "apply",
            "--dsn",
            "postgresql://public@example/gobby",
        ])
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(stderr.contains("unexpected argument '--dsn'"));
    Ok(())
}

#[test]
fn connection_errors_redact_dsn_credentials() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply"])
        .env(DATABASE_URL_ENV, SECRET_DSN)
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(stderr.contains("failed to connect to the Gobby PostgreSQL hub"));
    assert!(!stderr.contains("schema_user"));
    assert!(!stderr.contains("do-not-leak"));
    Ok(())
}

#[test]
fn destructive_apply_parses_newest_backup_before_connecting() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let home_path = home.path().canonicalize()?;
    let backup = home_path.join("backups/hub/20260805T120000Z");
    std::fs::create_dir_all(&backup)?;
    std::fs::write(backup.join("manifest.json"), "{}")?;

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--destructive"])
        .env("GOBBY_HOME", &home_path)
        .env(DATABASE_URL_ENV, SECRET_DSN)
        .output()?;

    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("invalid hub backup manifest"), "{stderr}");
    assert!(!stderr.contains("failed to connect"), "{stderr}");
    assert!(!stderr.contains("do-not-leak"), "{stderr}");
    Ok(())
}
