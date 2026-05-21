use std::path::PathBuf;

use anyhow::{Context as _, bail};
use postgres::{Client, NoTls};

use crate::schema;

const POSTGRES_DATABASE_URL_REF: &str = "keyring:gobby:postgres_database_url";

#[derive(Debug, Clone, PartialEq, Eq)]
struct BootstrapDatabase {
    hub_backend: String,
    database_url: Option<String>,
    database_url_ref: Option<String>,
}

/// Return Gobby home, respecting `GOBBY_HOME` when the daemon was configured with it.
pub fn gobby_home() -> anyhow::Result<PathBuf> {
    if let Some(home) = std::env::var_os("GOBBY_HOME") {
        return Ok(PathBuf::from(home));
    }
    Ok(dirs::home_dir()
        .context("cannot determine home directory")?
        .join(".gobby"))
}

pub fn bootstrap_path() -> anyhow::Result<PathBuf> {
    Ok(gobby_home()?.join("bootstrap.yaml"))
}

/// Resolve the PostgreSQL hub DSN from Gobby bootstrap config.
///
/// gcode intentionally has no local database fallback. The daemon process does not need
/// to be running, but the migrated PostgreSQL hub must be configured.
pub fn resolve_database_url() -> anyhow::Result<String> {
    let path = bootstrap_path()?;
    let contents = std::fs::read_to_string(&path).with_context(|| {
        format!(
            "missing Gobby bootstrap at {}. Run `gobby postgres migrate-from-sqlite` and cut over to the PostgreSQL hub first.",
            path.display()
        )
    })?;
    let bootstrap = parse_bootstrap_database(&contents)?;
    resolve_database_url_from_bootstrap(&bootstrap, resolve_keyring_database_url)
}

fn parse_bootstrap_database(contents: &str) -> anyhow::Result<BootstrapDatabase> {
    let yaml: serde_yaml::Value =
        serde_yaml::from_str(contents).context("failed to parse bootstrap.yaml")?;
    let Some(map) = yaml.as_mapping() else {
        bail!("bootstrap.yaml must be a mapping");
    };

    let get_string = |name: &str| -> anyhow::Result<Option<String>> {
        let key = serde_yaml::Value::String(name.to_string());
        match map.get(&key) {
            Some(value) => match value.as_str() {
                Some(text) if !text.trim().is_empty() => Ok(Some(text.to_string())),
                Some(_) | None => bail!("bootstrap.yaml field `{name}` must be a string"),
            },
            None => Ok(None),
        }
    };

    Ok(BootstrapDatabase {
        hub_backend: get_string("hub_backend")?.unwrap_or_else(|| "sqlite".to_string()),
        database_url: get_string("database_url")?,
        database_url_ref: get_string("database_url_ref")?,
    })
}

fn resolve_database_url_from_bootstrap(
    bootstrap: &BootstrapDatabase,
    keyring_resolver: impl Fn(&str) -> anyhow::Result<String>,
) -> anyhow::Result<String> {
    if bootstrap.hub_backend != "postgres" {
        bail!(
            "gcode requires `hub_backend: postgres` in bootstrap.yaml. Current hub_backend is `{}`. Run `gobby postgres migrate-from-sqlite` and cut over first.",
            bootstrap.hub_backend
        );
    }

    if let Some(database_url_ref) = bootstrap.database_url_ref.as_deref() {
        return keyring_resolver(database_url_ref);
    }

    if let Some(database_url) = bootstrap.database_url.as_deref() {
        return Ok(database_url.to_string());
    }

    bail!(
        "hub_backend=postgres requires `database_url_ref: {POSTGRES_DATABASE_URL_REF}` or an inline `database_url`"
    )
}

fn resolve_keyring_database_url(database_url_ref: &str) -> anyhow::Result<String> {
    let (service, username) = parse_database_url_ref(database_url_ref)?;
    keyring::use_native_store(false).context("failed to open OS keyring store")?;
    let store = keyring_core::get_default_store().context("OS keyring store is not configured")?;
    let entry = store
        .build(service, username, None)
        .with_context(|| format!("failed to open OS keyring entry {database_url_ref}"))?;
    let database_url = entry.get_password().with_context(|| {
        format!("failed to read database_url from OS keyring entry {database_url_ref}")
    })?;
    if database_url.trim().is_empty() {
        bail!("database_url_ref keyring entry {database_url_ref} is missing");
    }
    Ok(database_url)
}

fn parse_database_url_ref(database_url_ref: &str) -> anyhow::Result<(&str, &str)> {
    let parts: Vec<&str> = database_url_ref.splitn(3, ':').collect();
    if parts.as_slice() != ["keyring", "gobby", "postgres_database_url"] {
        bail!("database_url_ref must be {POSTGRES_DATABASE_URL_REF}");
    }
    Ok((parts[1], parts[2]))
}

pub fn connect_readwrite(database_url: &str) -> anyhow::Result<Client> {
    connect(database_url)
}

pub fn connect_readonly(database_url: &str) -> anyhow::Result<Client> {
    connect(database_url)
}

pub fn symbol_select_columns(alias: &str) -> String {
    let prefix = if alias.is_empty() {
        String::new()
    } else {
        format!("{alias}.")
    };
    format!(
        "{p}id, {p}project_id, {p}file_path, {p}name, {p}qualified_name, \
         {p}kind, {p}language, {p}byte_start::BIGINT AS byte_start, \
         {p}byte_end::BIGINT AS byte_end, {p}line_start::BIGINT AS line_start, \
         {p}line_end::BIGINT AS line_end, {p}signature, {p}docstring, \
         {p}parent_symbol_id, {p}content_hash, {p}summary, \
         {p}created_at::TEXT AS created_at, {p}updated_at::TEXT AS updated_at",
        p = prefix
    )
}

fn connect(database_url: &str) -> anyhow::Result<Client> {
    let mut client = Client::connect(database_url, NoTls)
        .context("failed to connect to the Gobby PostgreSQL hub")?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bootstrap(
        hub_backend: &str,
        database_url: Option<&str>,
        database_url_ref: Option<&str>,
    ) -> BootstrapDatabase {
        BootstrapDatabase {
            hub_backend: hub_backend.to_string(),
            database_url: database_url.map(str::to_string),
            database_url_ref: database_url_ref.map(str::to_string),
        }
    }

    #[test]
    fn postgres_bootstrap_resolves_keyring_ref() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(POSTGRES_DATABASE_URL_REF)),
            |reference| {
                assert_eq!(reference, POSTGRES_DATABASE_URL_REF);
                Ok("postgresql://gobby:secret@localhost:60891/gobby".to_string())
            },
        )
        .expect("resolve ref");

        assert_eq!(resolved, "postgresql://gobby:secret@localhost:60891/gobby");
    }

    #[test]
    fn postgres_bootstrap_prefers_keyring_ref_over_inline_url() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap(
                "postgres",
                Some("postgresql://inline/db"),
                Some(POSTGRES_DATABASE_URL_REF),
            ),
            |_| Ok("postgresql://keyring/db".to_string()),
        )
        .expect("resolve ref");

        assert_eq!(resolved, "postgresql://keyring/db");
    }

    #[test]
    fn postgres_bootstrap_accepts_inline_url() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", Some("postgresql://inline/db"), None),
            |_| unreachable!("keyring should not be used"),
        )
        .expect("resolve inline url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn sqlite_bootstrap_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(&bootstrap("sqlite", None, None), |_| {
            unreachable!("keyring should not be used")
        })
        .expect_err("sqlite backend must fail");

        assert!(err.to_string().contains("hub_backend: postgres"));
    }

    #[test]
    fn missing_postgres_dsn_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(&bootstrap("postgres", None, None), |_| {
            unreachable!("keyring should not be used")
        })
        .expect_err("missing dsn must fail");

        assert!(err.to_string().contains("database_url_ref"));
    }

    #[test]
    fn unsupported_database_url_ref_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(
            &bootstrap(
                "postgres",
                None,
                Some("keyring:other:postgres_database_url"),
            ),
            resolve_keyring_database_url,
        )
        .expect_err("unsupported ref must fail");

        assert!(err.to_string().contains(POSTGRES_DATABASE_URL_REF));
    }

    #[test]
    fn parse_bootstrap_database_reads_postgres_fields() {
        let parsed = parse_bootstrap_database(
            "hub_backend: postgres\n\
             database_url_ref: keyring:gobby:postgres_database_url\n",
        )
        .expect("parse bootstrap");

        assert_eq!(parsed.hub_backend, "postgres");
        assert_eq!(
            parsed.database_url_ref.as_deref(),
            Some(POSTGRES_DATABASE_URL_REF)
        );
    }
}
