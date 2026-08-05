use std::env;
use std::fs;
use std::sync::{Mutex, mpsc};
use std::time::Duration as StdDuration;

use postgres::{Client, Config, NoTls};
use time::format_description::well_known::Rfc3339;
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use super::assets::{BASELINE_VERSION, EmbeddedMigration, MIGRATIONS};
use super::gate::{
    BackupGateContext, SourceIdentity, VerifiedBackupManifest, parse_backup_manifest,
};
use super::runner::{SchemaError, SchemaRunner};

static RECOVERY_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 377,
    filename: "377_recovery_probe.sql",
    checksum: "d63e14df78da3519a30caf2dac74341ab5f0c9aa05f7bec58174ec0adf383159",
    sql: "-- gobby:non-transactional\nCREATE UNIQUE INDEX CONCURRENTLY schema_recovery_idx ON recovery_values(id);\n",
};
static RECOVERY_MIGRATIONS: &[EmbeddedMigration] = &[MIGRATIONS[0], RECOVERY_MIGRATION];

static DESTRUCTIVE_MIGRATION: EmbeddedMigration = EmbeddedMigration {
    version: 377,
    filename: "377_destructive_probe.sql",
    checksum: "c10820fc8be4c2bceab1610fd8372c8d864fd7c4a8985773cf903bae450b19e9",
    sql: "-- gobby:destructive\nCREATE TABLE gate_probe (id integer);\n",
};
static DESTRUCTIVE_MIGRATIONS: &[EmbeddedMigration] = &[MIGRATIONS[0], DESTRUCTIVE_MIGRATION];

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

fn current_schema_head() -> i32 {
    MIGRATIONS
        .last()
        .map_or(BASELINE_VERSION, |migration| migration.version)
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
fn lock_and_recovery_tests_repair_an_invalid_concurrent_index() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = test_database()? else {
        return Ok(());
    };
    install_baseline(&mut client)?;
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
    install_baseline(&mut client)?;
    let error =
        SchemaRunner::with_migrations_for_test(&mut client, "public", DESTRUCTIVE_MIGRATIONS)?
            .apply()
            .expect_err("default apply must halt at a destructive migration");
    assert!(error.to_string().contains("verified hub backup"));

    let fixture = include_str!("../../tests/fixtures/hub_backup_manifest/v2_roundtrip.json");
    let mut manifest = parse_backup_manifest(fixture)?;
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
        current_schema_head(),
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
        current_schema_head(),
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
