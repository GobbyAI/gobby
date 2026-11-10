use std::env;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use gobby_core::bootstrap::{bootstrap_path, postgres_database_url_from_bootstrap_file};
use gobby_core::degradation::redact_database_url;
use gobby_core::postgres::connect_readwrite;
use gobby_core::schema::{
    SchemaIdentityContract, SchemaRunner, VerificationReport,
    sweep_test_schemas as sweep_orphaned_test_schemas,
};
use gobby_daemon::front_door::compare;
use gobby_daemon::front_door::waivers::WaiverManifest;
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
    /// Summarize the front-door compare log; exits 1 on any diff, native_error or
    /// mismatch, and on an empty window.
    CompareReport {
        /// Count only records at or after this Unix time in seconds.
        #[arg(long)]
        since: Option<i64>,
    },
    /// Sample daemon health and host load into the fleet state file. Runs as its own
    /// process, outside the Python daemon, and keeps running across daemon restarts.
    #[cfg(unix)]
    FleetWatch {
        /// Take one sample, write the state file, and exit.
        #[arg(long)]
        once: bool,
        /// Project whose `project_policy` rows are read, in addition to global rows.
        /// Without it only global rows are read.
        #[arg(long)]
        project_id: Option<String>,
        /// Directory holding `daemon.log`. Defaults to `GOBBY_LOGGING_DIR`, then
        /// `<home>/logs`. The daemon's `logging.dir` in config is not read.
        #[arg(long)]
        logs_dir: Option<PathBuf>,
    },
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
    Preflight {
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
    let verifying = matches!(
        &cli.command,
        Command::Schema {
            command: SchemaCommand::Verify
        }
    );
    let result = match cli.command {
        Command::Schema {
            command: SchemaCommand::Version { json },
        } => print_schema_identity(json),
        Command::Schema {
            command: SchemaCommand::Apply { schema },
        } => apply_schema(schema.as_deref()),
        Command::Schema {
            command: SchemaCommand::Plan { schema },
        } => inspect_schema(schema.as_deref(), false),
        Command::Schema {
            command: SchemaCommand::Preflight { schema },
        } => inspect_schema(schema.as_deref(), true),
        Command::Schema {
            command: SchemaCommand::Verify,
        } => verify_schema(),
        Command::Schema {
            command: SchemaCommand::SweepTestSchemas { age_hours },
        } => sweep_test_schemas(age_hours),
        Command::Serve => gobby_daemon::serve::run(),
        Command::CompareReport { since } => compare_report(since),
        #[cfg(unix)]
        Command::FleetWatch {
            once,
            project_id,
            logs_dir,
        } => fleet_watch(once, project_id, logs_dir),
    };
    if let Err(error) = &result
        && verifying
        && transient_schema_failure(error)
    {
        eprintln!("{error}");
        std::process::exit(75);
    }
    result
}

fn compare_report(since: Option<i64>) -> Result<()> {
    let path = compare::log_path(&gobby_core::gobby_home()?);
    let manifest = WaiverManifest::embedded()?;
    let report = compare::report(&path, since, &manifest)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    if !report.passes() {
        std::process::exit(1);
    }
    Ok(())
}

/// Run the fleet watcher on a single-threaded runtime: one sampling loop, no daemon state.
/// The home, log directory and bootstrap follow the watcher's own resolution, which mirrors
/// the Python daemon, so an empty or `~`-prefixed `GOBBY_HOME` reads the same files.
#[cfg(unix)]
fn fleet_watch(once: bool, project_id: Option<String>, logs_dir: Option<PathBuf>) -> Result<()> {
    use gobby_daemon::fleet_health::paths::{gobby_home, logs_dir as resolve_logs_dir};

    let user_home = env::var_os("HOME").map(PathBuf::from);
    let home = gobby_home(env::var_os("GOBBY_HOME").as_deref(), user_home.as_deref())
        .context("cannot determine the Gobby home: set GOBBY_HOME or HOME")?;
    let logs = resolve_logs_dir(
        logs_dir.as_deref(),
        env::var_os("GOBBY_LOGGING_DIR").as_deref(),
        &home,
        user_home.as_deref(),
    );
    if let Some(id) = &project_id {
        uuid::Uuid::parse_str(id).context("--project-id must be a project UUID")?;
    }
    let bootstrap = home.join("bootstrap.yaml");
    let endpoint = gobby_core::bootstrap::read_daemon_endpoint_at(&bootstrap)?;
    let database_url = match resolve_database_url_from(Some(&bootstrap)) {
        Ok(url) => Some(url),
        Err(error) => {
            eprintln!("fleet-watch: {error:#}; thresholds fall back to defaults");
            None
        }
    };
    let config = gobby_daemon::fleet_health::watch::WatchConfig::under_home(
        &home,
        &logs,
        &endpoint.host,
        endpoint.port,
        database_url,
        project_id,
    );
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(gobby_daemon::fleet_health::watch::run(config, once))
}

fn transient_schema_failure(error: &anyhow::Error) -> bool {
    if error.is::<SchemaConnectUnavailable>() {
        return true;
    }
    error
        .downcast_ref::<deadpool_postgres::tokio_postgres::Error>()
        .is_some_and(|error| {
            error.is_closed()
                || error.code().is_none_or(|code| {
                    code.code().starts_with("08")
                        || matches!(code.code(), "57P01" | "57P02" | "57P03")
                })
        })
}

#[derive(Debug)]
struct SchemaConnectUnavailable(String);

impl std::fmt::Display for SchemaConnectUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "failed to connect to the Gobby PostgreSQL hub at {}",
            self.0
        )
    }
}

impl std::error::Error for SchemaConnectUnavailable {}

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

/// Report pending work, optionally proving it inside a rolled-back transaction.
///
/// Mirrors `apply_schema`'s connection handling only. It never calls
/// `verify_database_identity` and never calls `SchemaRunner::verify`: both bail
/// exactly when the database head differs from the embedded head, which is the
/// normal state of every migration-owing restart and of every cutover.
fn inspect_schema(schema: Option<&str>, data_preflight: bool) -> Result<()> {
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
    let mut runner = SchemaRunner::new(&mut client, &schema)?;
    let (action, report) = if data_preflight {
        ("preflight", runner.preflight()?)
    } else {
        ("plan", runner.plan()?)
    };
    let versions = report
        .pending_versions
        .iter()
        .map(i32::to_string)
        .collect::<Vec<_>>()
        .join(", ");
    println!(
        "schema {schema} {action}: database v{}, code v{}, baseline_pending={}, \
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
    let mut client = connect_readwrite(&database_url)
        .map_err(|_| SchemaConnectUnavailable(redact_database_url(&database_url)))?;
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
    resolve_database_url_from(bootstrap_path().as_deref())
}

/// The environment override first, then the bootstrap file at `bootstrap`.
fn resolve_database_url_from(bootstrap: Option<&Path>) -> Result<String> {
    if let Some(database_url) = env::var_os(DATABASE_URL_ENV) {
        let database_url = database_url
            .into_string()
            .map_err(|_| anyhow::anyhow!("{DATABASE_URL_ENV} must be valid UTF-8"))?;
        if !database_url.trim().is_empty() {
            return Ok(database_url);
        }
    }
    let path = bootstrap.context("cannot resolve Gobby bootstrap path")?;
    postgres_database_url_from_bootstrap_file(path)?
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
