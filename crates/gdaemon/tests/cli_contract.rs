use assert_cmd::Command;

const EXPECTED_IDENTITY_ENV: &str = "GOBBY_EXPECTED_SCHEMA_IDENTITY";
const DATABASE_URL_ENV: &str = "GOBBY_DATABASE_URL";
const SECRET_DSN: &str = "postgresql://schema_user:do-not-leak@127.0.0.1:1/gobby";

fn embedded_identity_json() -> anyhow::Result<String> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "version", "--json"])
        .output()?;
    assert!(output.status.success(), "{output:?}");
    Ok(String::from_utf8(output.stdout)?.trim().to_owned())
}

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
    assert_eq!(identity["baseline_version"], 375);
    assert_eq!(identity["latest_version"], 406);
    assert_eq!(
        identity["baseline_checksum"],
        "ec222a7f8b3c486abfff05eda4ed02995d272a132ad2fdadb1dd90edbccb2ce1"
    );
    assert_eq!(
        identity["latest_checksum"],
        "95a19015d5804ef3f82390739898f5019ced3572135fe8ce6f8a55b5b26ca3cb"
    );
    assert_eq!(
        identity["assets_root_hash"],
        "6645a22fca29ced97cf113bb022edf3e5544b0d83679293c1acafa1495d909e3"
    );
    assert_eq!(identity["runner_protocol"], 1);
    assert_eq!(
        identity["assets_root_hash"].as_str().map(str::len),
        Some(64)
    );
    Ok(())
}

#[test]
fn apply_rejects_mismatched_identity_before_connecting() -> anyhow::Result<()> {
    let mut expected: serde_json::Value = serde_json::from_str(&embedded_identity_json()?)?;
    expected["latest_version"] = serde_json::json!(999);

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply"])
        .env(EXPECTED_IDENTITY_ENV, serde_json::to_string(&expected)?)
        .env(DATABASE_URL_ENV, SECRET_DSN)
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(stderr.contains("expected schema identity does not match embedded identity"));
    assert!(!stderr.contains("failed to connect"));
    assert!(!stderr.contains("schema_user"));
    assert!(!stderr.contains("do-not-leak"));
    Ok(())
}

#[test]
fn verify_rejects_mismatched_identity_before_connecting() -> anyhow::Result<()> {
    let mut expected: serde_json::Value = serde_json::from_str(&embedded_identity_json()?)?;
    expected["latest_version"] = serde_json::json!(999);

    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "verify"])
        .env(EXPECTED_IDENTITY_ENV, serde_json::to_string(&expected)?)
        .env(DATABASE_URL_ENV, SECRET_DSN)
        .output()?;
    let stderr = String::from_utf8(output.stderr)?;

    assert!(!output.status.success());
    assert!(stderr.contains("expected schema identity does not match embedded identity"));
    assert!(!stderr.contains("failed to connect"));
    assert!(!stderr.contains("schema_user"));
    assert!(!stderr.contains("do-not-leak"));
    Ok(())
}

#[test]
fn apply_rejects_malicious_schema_before_connecting() -> anyhow::Result<()> {
    let output = Command::cargo_bin("gdaemon")?
        .args(["schema", "apply", "--schema", "bad\";drop schema public;--"])
        .env(EXPECTED_IDENTITY_ENV, embedded_identity_json()?)
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
        .env(EXPECTED_IDENTITY_ENV, embedded_identity_json()?)
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
        .env(EXPECTED_IDENTITY_ENV, embedded_identity_json()?)
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
