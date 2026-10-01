use std::env;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use gobby_core::bootstrap::{bootstrap_path, postgres_database_url_from_bootstrap_file};
use gobby_core::degradation::redact_database_url;
use gobby_core::postgres::connect_readwrite;
use gobby_core::schema::{
    SchemaIdentityContract, SchemaRunner, VerificationReport,
    sweep_test_schemas as sweep_orphaned_test_schemas,
};
use time::OffsetDateTime;

const DATABASE_URL_ENV: &str = "GOBBY_DATABASE_URL";

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
    /// Own the public HTTP and WS ports and proxy to the Python backend.
    Serve,
}

#[derive(Debug, Subcommand)]
enum SchemaCommand {
    Apply {
        #[arg(long)]
        schema: Option<String>,
    },
    Plan {
        #[arg(long)]
        schema: Option<String>,
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

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Schema {
            command: SchemaCommand::Version { json },
        } => print_schema_identity(json),
        Command::Schema {
            command: SchemaCommand::Apply { schema },
        } => apply_schema(schema.as_deref()),
        Command::Schema {
            command: SchemaCommand::Plan { schema },
        } => plan_schema(schema.as_deref()),
        Command::Schema {
            command: SchemaCommand::Verify,
        } => verify_schema(),
        Command::Schema {
            command: SchemaCommand::SweepTestSchemas { age_hours },
        } => sweep_test_schemas(age_hours),
        Command::Serve => gobby_daemon::serve::run(),
    }
}

fn sweep_test_schemas(age_hours: u64) -> Result<()> {
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
    let schema = client
        .query_one("SELECT current_schema()", &[])?
        .get::<_, Option<String>>(0)
        .context("PostgreSQL connection has no current schema")?;
    verify_database_identity(&mut SchemaRunner::new(&mut client, &schema)?, true)?;
    let dropped = sweep_orphaned_test_schemas(&mut client, cutoff_epoch)?;
    println!("swept {dropped} orphaned PostgreSQL test schema(s)");
    Ok(())
}

fn apply_schema(schema: Option<&str>) -> Result<()> {
    if let Some(schema) = schema {
        validate_schema_name(schema)?;
    }
    let database_url = resolve_database_url()?;
    let mut client = connect_readwrite(&database_url).map_err(|_| {
        anyhow::anyhow!(
            "failed to connect to the Gobby PostgreSQL hub at {}",
            redact_database_url(&database_url)
        )
    })?;
    let schema = match schema {
        Some(schema) => schema.to_owned(),
        None => client
            .query_one("SELECT current_schema()", &[])?
            .get::<_, Option<String>>(0)
            .context("PostgreSQL connection has no current schema")?,
    };
    validate_schema_name(&schema)?;
    let report = SchemaRunner::new(&mut client, &schema)?.apply()?;
    println!(
        "schema {schema} ready (baseline_applied={}, migrations_applied={})",
        report.baseline_applied, report.migrations_applied
    );
    Ok(())
}

/// Report what `schema apply` would do against this database, writing nothing.
///
/// Mirrors `apply_schema`'s connection handling only. It never calls
/// `verify_database_identity` and never calls `SchemaRunner::verify`: both bail
/// exactly when the database head differs from the embedded head, which is the
/// normal state of every migration-owing restart and of every cutover.
fn plan_schema(schema: Option<&str>) -> Result<()> {
    if let Some(schema) = schema {
        validate_schema_name(schema)?;
    }
    let database_url = resolve_database_url()?;
    let mut client = connect_readwrite(&database_url).map_err(|_| {
        anyhow::anyhow!(
            "failed to connect to the Gobby PostgreSQL hub at {}",
            redact_database_url(&database_url)
        )
    })?;
    let schema = match schema {
        Some(schema) => schema.to_owned(),
        None => client
            .query_one("SELECT current_schema()", &[])?
            .get::<_, Option<String>>(0)
            .context("PostgreSQL connection has no current schema")?,
    };
    validate_schema_name(&schema)?;
    let report = SchemaRunner::new(&mut client, &schema)?.plan()?;
    let versions = report
        .pending_versions
        .iter()
        .map(i32::to_string)
        .collect::<Vec<_>>()
        .join(", ");
    println!(
        "schema {schema} plan: database v{}, code v{}, baseline_pending={}, \
         pending_migrations={} [{versions}]",
        report.database_head,
        report.code_head,
        report.baseline_pending,
        report.pending_versions.len()
    );
    Ok(())
}

fn verify_schema() -> Result<()> {
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
    let report = verify_database_identity(&mut SchemaRunner::new(&mut client, &schema)?, false)?
        .context("database schema is not initialized")?;
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

fn verify_database_identity(
    runner: &mut SchemaRunner<'_>,
    allow_uninitialized: bool,
) -> Result<Option<VerificationReport>> {
    let database_version = runner.current_version()?;
    // Pytest sweeps a fresh isolated database before applying its first worker schema.
    // With no receipts there is no database identity yet, so only sweeping may proceed.
    if allow_uninitialized && database_version == 0 {
        return Ok(None);
    }
    let embedded_version = SchemaIdentityContract::embedded().latest_version;
    if database_version != embedded_version {
        anyhow::bail!(
            "binary-embedded schema identity v{embedded_version} does not match database schema v{database_version}"
        );
    }
    runner.verify().map(Some).map_err(|error| {
        anyhow::anyhow!(
            "binary-embedded schema identity v{embedded_version} does not match database schema v{database_version}: {error}"
        )
    })
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
