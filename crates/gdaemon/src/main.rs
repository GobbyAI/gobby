use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use gobby_core::bootstrap::{bootstrap_path, postgres_database_url_from_bootstrap_file};
use gobby_core::degradation::redact_database_url;
use gobby_core::gobby_home;
use gobby_core::postgres::{connect_readwrite, is_lock_timeout};
use gobby_core::schema::{
    BackupGateContext, HubBackupManifest, SchemaRunner, SourceIdentity, VerifiedBackupManifest,
    parse_backup_manifest, schema_identity, sweep_test_schemas as sweep_orphaned_test_schemas,
};
use serde::{Deserialize, Serialize};
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

const EXPECTED_IDENTITY_ENV: &str = "GOBBY_EXPECTED_SCHEMA_IDENTITY";
const DATABASE_URL_ENV: &str = "GOBBY_DATABASE_URL";
const BACKUP_MANIFEST_NAME: &str = "manifest.json";

#[derive(Debug, Parser)]
#[command(name = "gdaemon", version, about = "Gobby schema authority")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Schema {
        #[command(subcommand)]
        command: SchemaCommand,
    },
}

#[derive(Debug, Subcommand)]
enum SchemaCommand {
    Apply {
        #[arg(long)]
        schema: Option<String>,
        #[arg(long)]
        destructive: bool,
    },
    SweepTestSchemas {
        #[arg(long, default_value_t = 1)]
        age_hours: u64,
    },
    Verify,
    Version {
        #[arg(long)]
        json: bool,
    },
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct SchemaIdentityContract {
    runner_protocol: u32,
    baseline_version: i32,
    baseline_checksum: String,
    latest_version: i32,
    latest_checksum: String,
    assets_root_hash: String,
}

impl SchemaIdentityContract {
    fn embedded() -> Self {
        let identity = schema_identity();
        Self {
            runner_protocol: identity.runner_protocol_version,
            baseline_version: identity.baseline.version,
            baseline_checksum: identity.baseline.checksum.to_owned(),
            latest_version: identity.latest_asset.version,
            latest_checksum: identity.latest_asset.checksum.to_owned(),
            assets_root_hash: identity.root_hash,
        }
    }
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Schema {
            command: SchemaCommand::Version { json },
        } => print_schema_identity(json),
        Command::Schema {
            command:
                SchemaCommand::Apply {
                    schema,
                    destructive,
                },
        } => apply_schema(schema.as_deref(), destructive),
        Command::Schema {
            command: SchemaCommand::Verify,
        } => verify_schema(),
        Command::Schema {
            command: SchemaCommand::SweepTestSchemas { age_hours },
        } => sweep_test_schemas(age_hours),
    }
}

fn sweep_test_schemas(age_hours: u64) -> Result<()> {
    enforce_expected_identity()?;
    let age_seconds =
        i64::try_from(age_hours.saturating_mul(60 * 60)).context("--age-hours is too large")?;
    let cutoff_epoch = OffsetDateTime::now_utc()
        .unix_timestamp()
        .checked_sub(age_seconds)
        .context("--age-hours is too large")?;
    let database_url = resolve_database_url()?;
    let mut client = connect_readwrite(&database_url).map_err(|_| {
        anyhow::anyhow!(
            "failed to connect to the Gobby PostgreSQL hub at {}",
            redact_database_url(&database_url)
        )
    })?;
    let dropped = sweep_orphaned_test_schemas(&mut client, cutoff_epoch)?;
    println!("swept {dropped} orphaned PostgreSQL test schema(s)");
    Ok(())
}

fn apply_schema(schema: Option<&str>, destructive: bool) -> Result<()> {
    if let Some(schema) = schema {
        validate_schema_name(schema)?;
    }
    enforce_expected_identity()?;
    let database_url = resolve_database_url()?;
    let backup = destructive.then(load_newest_backup_manifest).transpose()?;
    let mut client = connect_readwrite(&database_url).map_err(|_| {
        anyhow::anyhow!(
            "failed to connect to the Gobby PostgreSQL hub at {}",
            redact_database_url(&database_url)
        )
    })?;
    let _epoch_lease = if destructive {
        let setting: String = client
            .query_one(
                "SELECT current_setting('gobby.maintenance_epoch', true)",
                &[],
            )
            .context("failed to read gobby.maintenance_epoch")?
            .get::<_, Option<String>>(0)
            .unwrap_or_default();
        if setting.trim().is_empty() {
            anyhow::bail!(
                "destructive schema apply requires an open maintenance epoch; \
                 run `gobby hub-maintenance run schema-apply`"
            );
        }
        let table_exists: bool = client
            .query_one("SELECT to_regclass('maintenance_epochs') IS NOT NULL", &[])
            .context("failed to inspect maintenance_epochs")?
            .get(0);
        if !table_exists {
            anyhow::bail!(
                "destructive schema apply requires an open maintenance epoch; \
                 run `gobby hub-maintenance run schema-apply`"
            );
        }
        Some(hold_open_maintenance_epoch_lease(&database_url, &setting)?)
    } else {
        None
    };
    let schema = match schema {
        Some(schema) => schema.to_owned(),
        None => client
            .query_one("SELECT current_schema()", &[])?
            .get::<_, Option<String>>(0)
            .context("PostgreSQL connection has no current schema")?,
    };
    validate_schema_name(&schema)?;
    let report = if let Some((backup_root, manifest)) = backup {
        let row = client
            .query_one(
                "SELECT (pg_control_system()).system_identifier::text, current_database(), oid \
                 FROM pg_database WHERE datname = current_database()",
                &[],
            )
            .context("failed to read PostgreSQL source identity")?;
        let current_identity = SourceIdentity {
            pg_system_identifier: row.get(0),
            database_name: row.get(1),
            database_oid: row.get(2),
        };
        let has_migrations: bool = client
            .query_one(
                "SELECT EXISTS (\
                    SELECT 1 FROM pg_class c \
                    JOIN pg_namespace n ON n.oid = c.relnamespace \
                    WHERE n.nspname = $1 AND c.relname = 'schema_migrations' \
                      AND c.relkind IN ('r', 'p')\
                )",
                &[&schema],
            )
            .context("failed to inspect schema migration state")?
            .get(0);
        let current_head = if has_migrations {
            let table = format!("\"{schema}\".\"schema_migrations\"");
            client
                .query_one(
                    &format!("SELECT COALESCE(MAX(version), 0) FROM {table}"),
                    &[],
                )
                .context("failed to read schema migration head")?
                .get(0)
        } else {
            0
        };
        let context = BackupGateContext::new(
            &backup_root,
            &current_identity,
            current_head,
            OffsetDateTime::now_utc(),
        );
        let verified = VerifiedBackupManifest::verify(manifest, &context)
            .context("hub backup manifest failed the destructive migration gate")?;
        SchemaRunner::new(&mut client, &schema)?.apply_with_backup(&verified)?
    } else {
        SchemaRunner::new(&mut client, &schema)?.apply()?
    };
    println!(
        "schema {schema} ready (baseline_applied={}, migrations_applied={})",
        report.baseline_applied, report.migrations_applied
    );
    Ok(())
}

fn hold_open_maintenance_epoch_lease(
    database_url: &str,
    epoch_id: &str,
) -> Result<impl Drop + use<>> {
    // Apply commits per migration on the runner connection. Hold FOR UPDATE
    // here so release_maintenance_epoch waits until apply returns.
    let mut lease = connect_readwrite(database_url).map_err(|_| {
        anyhow::anyhow!(
            "failed to connect to the Gobby PostgreSQL hub at {}",
            redact_database_url(database_url)
        )
    })?;
    lease
        .batch_execute(
            "BEGIN; SET LOCAL idle_in_transaction_session_timeout = 0; \
             SET LOCAL lock_timeout = '5s'",
        )
        .context("failed to open a maintenance-epoch lease")?;
    let locked = match lease.query(
        "SELECT 1 FROM maintenance_epochs \
         WHERE id::text = $1 AND released_at IS NULL \
         FOR UPDATE",
        &[&epoch_id],
    ) {
        Ok(rows) => rows,
        Err(error) => {
            let _ = lease.batch_execute("ROLLBACK");
            if is_lock_timeout(&error) {
                return Err(error)
                    .context("timed out acquiring a lock on the open maintenance epoch");
            }
            return Err(error).context("failed to acquire the open maintenance epoch");
        }
    };
    if locked.is_empty() {
        let _ = lease.batch_execute("ROLLBACK");
        anyhow::bail!(
            "gobby.maintenance_epoch={epoch_id} is not an open maintenance epoch; \
             run `gobby hub-maintenance run schema-apply`"
        );
    }
    Ok(lease)
}

fn load_newest_backup_manifest() -> Result<(PathBuf, HubBackupManifest)> {
    let backup_root = gobby_home()?.join("backups/hub");
    refuse_symlink_traversal(&backup_root)?;
    let mut candidates = Vec::new();
    for entry in fs::read_dir(&backup_root).with_context(|| {
        format!(
            "no hub backup manifests found under {}",
            backup_root.display()
        )
    })? {
        let entry = entry.context("failed to inspect hub backup directory")?;
        if entry.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        let file_type = entry
            .file_type()
            .context("failed to inspect hub backup entry")?;
        if file_type.is_symlink() {
            anyhow::bail!(
                "hub backup path contains a symlink: {}",
                entry.path().display()
            );
        }
        if !file_type.is_dir() {
            continue;
        }
        let manifest_path = entry.path().join(BACKUP_MANIFEST_NAME);
        let metadata = match fs::symlink_metadata(&manifest_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error).context("failed to inspect hub backup manifest"),
        };
        if metadata.file_type().is_symlink() {
            anyhow::bail!(
                "hub backup manifest is a symlink: {}",
                manifest_path.display()
            );
        }
        if !metadata.is_file() {
            continue;
        }
        let payload = fs::read_to_string(&manifest_path)
            .with_context(|| format!("failed to read {}", manifest_path.display()))?;
        let manifest = parse_backup_manifest(&payload).map_err(|error| {
            anyhow::anyhow!(
                "invalid hub backup manifest {}: {error}",
                manifest_path.display()
            )
        })?;
        let created_at = OffsetDateTime::parse(&manifest.created_at, &Rfc3339)
            .with_context(|| format!("invalid created_at in {}", manifest_path.display()))?;
        candidates.push((created_at, entry.path(), manifest));
    }
    candidates
        .into_iter()
        .max_by_key(|(created_at, _, _)| *created_at)
        .map(|(_, root, manifest)| (root, manifest))
        .with_context(|| {
            format!(
                "no hub backup manifests found under {}",
                backup_root.display()
            )
        })
}

fn refuse_symlink_traversal(path: &Path) -> Result<()> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        let metadata = fs::symlink_metadata(&current)
            .with_context(|| format!("failed to inspect {}", current.display()))?;
        if metadata.file_type().is_symlink() {
            anyhow::bail!("hub backup path contains a symlink: {}", current.display());
        }
    }
    Ok(())
}

fn verify_schema() -> Result<()> {
    enforce_expected_identity()?;
    let database_url = resolve_database_url()?;
    let mut client = connect_readwrite(&database_url).map_err(|_| {
        anyhow::anyhow!(
            "failed to connect to the Gobby PostgreSQL hub at {}",
            redact_database_url(&database_url)
        )
    })?;
    let schema = client
        .query_one("SELECT current_schema()", &[])?
        .get::<_, Option<String>>(0)
        .context("PostgreSQL connection has no current schema")?;
    let report = SchemaRunner::new(&mut client, &schema)?.verify()?;
    println!(
        "schema verified (receipts={}, seed_rows={}, catalog_objects={})",
        report.checked_receipts, report.checked_seed_rows, report.checked_catalog_objects
    );
    Ok(())
}

fn validate_schema_name(schema: &str) -> Result<()> {
    let mut bytes = schema.bytes();
    let valid = (1..=63).contains(&schema.len())
        && matches!(bytes.next(), Some(byte) if byte.is_ascii_lowercase() || byte == b'_')
        && bytes.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_');
    if valid {
        Ok(())
    } else {
        anyhow::bail!("invalid PostgreSQL schema name; expected ^[a-z_][a-z0-9_]{{0,62}}$")
    }
}

fn enforce_expected_identity() -> Result<()> {
    let payload = env::var(EXPECTED_IDENTITY_ENV)
        .with_context(|| format!("{EXPECTED_IDENTITY_ENV} is required for schema apply"))?;
    let expected: SchemaIdentityContract = serde_json::from_str(&payload)
        .context("expected schema identity is not valid six-field JSON")?;
    let embedded = SchemaIdentityContract::embedded();
    if expected != embedded {
        anyhow::bail!(
            "expected schema identity does not match embedded identity (expected={expected:?}, embedded={embedded:?})"
        );
    }
    Ok(())
}

fn resolve_database_url() -> Result<String> {
    if let Some(database_url) = env::var_os(DATABASE_URL_ENV) {
        let database_url = database_url
            .into_string()
            .map_err(|_| anyhow::anyhow!("{DATABASE_URL_ENV} must be valid UTF-8"))?;
        if !database_url.trim().is_empty() {
            return Ok(database_url);
        }
    }
    let path = bootstrap_path().context("cannot resolve Gobby bootstrap path")?;
    postgres_database_url_from_bootstrap_file(&path)?
        .context("database_url is missing from bootstrap.yaml")
}

fn print_schema_identity(json: bool) -> Result<()> {
    let identity = SchemaIdentityContract::embedded();
    if json {
        println!("{}", serde_json::to_string(&identity)?);
    } else {
        println!("runner protocol: {}", identity.runner_protocol);
        println!(
            "baseline: v{} {}",
            identity.baseline_version, identity.baseline_checksum
        );
        println!(
            "latest: v{} {}",
            identity.latest_version, identity.latest_checksum
        );
        println!("assets root: {}", identity.assets_root_hash);
    }
    Ok(())
}
