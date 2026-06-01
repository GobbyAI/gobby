use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context as _, anyhow, bail};
use gobby_core::provisioning::{GCORE_CONFIG_FILENAME, StandaloneConfig};
use postgres::{Client, GenericClient};
use serde::Deserialize;

use crate::models::{CallRelation, CallTargetKind, ImportRelation, Symbol};
use crate::schema;

const GCODE_DATABASE_URL_ENV: &str = "GCODE_DATABASE_URL";
const GOBBY_POSTGRES_DSN_ENV: &str = "GOBBY_POSTGRES_DSN";
const GCODE_CONFIG_FILENAME: &str = "gcode.yaml";
const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
const BROKER_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug, Deserialize)]
struct BrokerDatabaseUrlResponse {
    database_url: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct BootstrapDatabase {
    hub_backend: String,
    database_url: Option<String>,
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

/// Resolve the PostgreSQL hub DSN from explicit overrides or Gobby bootstrap config.
///
/// gcode intentionally has no local database fallback. It asks the long-lived daemon
/// broker first, then falls back to explicit DSN sources for daemonless operation.
pub fn resolve_database_url() -> anyhow::Result<String> {
    let home = gobby_home()?;
    resolve_database_url_from_sources_with_identity_and_reachability(
        &home,
        |bootstrap_path| resolve_brokered_database_url_at(&home, bootstrap_path),
        |name| std::env::var(name).ok(),
        |url| gobby_core::postgres::connect_readonly(url).is_ok(),
        gobby_core::provisioning::probe_postgres_hub_identity,
    )
}

#[cfg(test)]
fn resolve_database_url_from_sources(
    home: &Path,
    broker_resolver: impl Fn(&Path) -> anyhow::Result<String>,
    get_var: impl FnMut(&str) -> Option<String>,
    database_reachable: impl FnMut(&str) -> bool,
) -> anyhow::Result<String> {
    resolve_database_url_from_sources_with_identity_and_reachability(
        home,
        broker_resolver,
        get_var,
        database_reachable,
        gobby_core::provisioning::probe_postgres_hub_identity,
    )
}

#[cfg(test)]
fn resolve_database_url_from_sources_with_identity(
    home: &Path,
    broker_resolver: impl Fn(&Path) -> anyhow::Result<String>,
    get_var: impl FnMut(&str) -> Option<String>,
    database_reachable: impl FnMut(&str) -> bool,
    identity_probe: impl FnMut(&str) -> anyhow::Result<gobby_core::provisioning::HubIdentityProbeResult>,
) -> anyhow::Result<String> {
    resolve_database_url_from_sources_with_identity_and_reachability(
        home,
        broker_resolver,
        get_var,
        database_reachable,
        identity_probe,
    )
}

fn resolve_database_url_from_sources_with_identity_and_reachability(
    home: &Path,
    broker_resolver: impl Fn(&Path) -> anyhow::Result<String>,
    get_var: impl FnMut(&str) -> Option<String>,
    mut database_reachable: impl FnMut(&str) -> bool,
    mut identity_probe: impl FnMut(
        &str,
    )
        -> anyhow::Result<gobby_core::provisioning::HubIdentityProbeResult>,
) -> anyhow::Result<String> {
    let path = home.join("bootstrap.yaml");

    if let Some(database_url) = resolve_database_url_from_env(get_var) {
        return Ok(database_url);
    }

    let gcore_database_url = resolve_database_url_from_gcore_config(home)?;

    if let Ok(database_url) = broker_resolver(&path) {
        if let Some(resolution) = gobby_core::provisioning::resolve_recorded_hub_database_url(
            gcore_database_url.as_deref(),
            Some(&database_url),
            &mut database_reachable,
            &mut identity_probe,
        )? {
            return Ok(resolution.database_url);
        }
        return Ok(database_url);
    }

    if let Some(database_url) = resolve_database_url_from_bootstrap_file(&path)? {
        if let Some(resolution) = gobby_core::provisioning::resolve_recorded_hub_database_url(
            gcore_database_url.as_deref(),
            Some(&database_url),
            &mut database_reachable,
            &mut identity_probe,
        )? {
            return Ok(resolution.database_url);
        }
        return Ok(database_url);
    }

    if let Some(database_url) = gcore_database_url {
        return Ok(database_url);
    }

    if let Some(database_url) =
        resolve_database_url_from_config_file(&home.join(GCODE_CONFIG_FILENAME))?
    {
        return Ok(database_url);
    }

    bail!(
        "missing Gobby PostgreSQL configuration. Run `gcode setup --standalone`, set {GCODE_DATABASE_URL_ENV}, or configure the Gobby daemon bootstrap."
    )
}

fn resolve_database_url_from_bootstrap_file(path: &Path) -> anyhow::Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
    }
    let contents = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read Gobby bootstrap at {}", path.display()))?;
    let bootstrap = parse_bootstrap_database(&contents)?;
    resolve_database_url_from_bootstrap(&bootstrap).map(Some)
}

fn resolve_database_url_from_gcore_config(home: &Path) -> anyhow::Result<Option<String>> {
    let Some(config) = StandaloneConfig::read_at(&home.join(GCORE_CONFIG_FILENAME))? else {
        return Ok(None);
    };
    Ok(config
        .get("databases.postgres.dsn")
        .and_then(|value| non_empty_trimmed(Some(value.to_string()))))
}

fn resolve_database_url_from_env(
    mut get_var: impl FnMut(&str) -> Option<String>,
) -> Option<String> {
    for name in [GCODE_DATABASE_URL_ENV, GOBBY_POSTGRES_DSN_ENV] {
        if let Some(value) = non_empty_trimmed(get_var(name)) {
            return Some(value);
        }
    }
    None
}

fn resolve_database_url_from_config_file(path: &Path) -> anyhow::Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
    }
    let contents = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read {}", path.display()))?;
    parse_gcode_config_database_url(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))
}

fn parse_gcode_config_database_url(contents: &str) -> anyhow::Result<Option<String>> {
    let yaml: serde_yaml::Value = serde_yaml::from_str(contents)?;
    let Some(map) = yaml.as_mapping() else {
        if yaml.is_null() {
            return Ok(None);
        }
        bail!("gcode.yaml must be a mapping");
    };

    let key = serde_yaml::Value::String("database_url".to_string());
    match map.get(&key) {
        Some(value) => match value.as_str() {
            Some(text) => Ok(non_empty_trimmed(Some(text.to_string()))),
            None => bail!("gcode.yaml field `database_url` must be a string"),
        },
        None => Ok(None),
    }
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

    let database_url_ref = get_string("database_url_ref")?;
    if database_url_ref.is_some() {
        bail!(
            "database_url_ref is no longer supported in bootstrap.yaml; store the local PostgreSQL DSN in database_url"
        );
    }

    Ok(BootstrapDatabase {
        hub_backend: get_string("hub_backend")?
            .context("bootstrap.yaml must include `hub_backend: postgres`")?,
        database_url: get_string("database_url")?,
    })
}

fn resolve_database_url_from_bootstrap(bootstrap: &BootstrapDatabase) -> anyhow::Result<String> {
    if bootstrap.hub_backend != "postgres" {
        bail!(
            "gcode requires `hub_backend: postgres` in bootstrap.yaml. Current hub_backend is `{}`. Configure the Gobby PostgreSQL hub before running gcode.",
            bootstrap.hub_backend
        );
    }

    if let Some(database_url) = bootstrap.database_url.as_deref() {
        return Ok(database_url.to_string());
    }

    bail!("hub_backend=postgres requires `database_url` in bootstrap.yaml")
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    let trimmed = value.as_ref()?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn resolve_brokered_database_url_at(
    gobby_home: &Path,
    bootstrap_path: &Path,
) -> anyhow::Result<String> {
    let token = read_local_cli_token_at(gobby_home)?;
    let daemon_url = gobby_core::daemon_url::daemon_url_at(bootstrap_path);
    request_broker_database_url(&daemon_url, &token)
}

fn read_local_cli_token_at(gobby_home: &Path) -> anyhow::Result<String> {
    let path = gobby_home.join(LOCAL_CLI_TOKEN_FILENAME);
    let token = std::fs::read_to_string(&path)
        .with_context(|| format!("missing local CLI token at {}", path.display()))?;
    let token = token.trim().to_string();
    if token.is_empty() {
        bail!("local CLI token at {} is empty", path.display());
    }
    Ok(token)
}

fn request_broker_database_url(daemon_url: &str, token: &str) -> anyhow::Result<String> {
    let url = format!(
        "{}/api/local/runtime/database-url",
        daemon_url.trim_end_matches('/')
    );
    let agent = ureq::AgentBuilder::new().timeout(BROKER_TIMEOUT).build();
    let response = agent
        .post(&url)
        .set("X-Gobby-Local-Token", token)
        .call()
        .map_err(|err| anyhow!("database_url broker request failed: {err}"))?;
    let body: BrokerDatabaseUrlResponse = response
        .into_json()
        .context("database_url broker response was not valid JSON")?;
    let database_url = body.database_url.trim().to_string();
    if database_url.is_empty() {
        bail!("database_url broker response was empty");
    }
    Ok(database_url)
}

/// Open a connection for command paths that may write to the hub.
///
/// This currently shares the same connection logic as read-only callers, but
/// keeping the intent explicit preserves a routing point for future pools,
/// permissions, or replicas.
pub fn connect_readwrite(database_url: &str) -> anyhow::Result<Client> {
    let mut client = gobby_core::postgres::connect_readwrite(database_url)?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

/// Open a connection for command paths that only read from the hub.
///
/// This currently shares the same connection logic as read-write callers, but
/// keeping the intent explicit preserves a routing point for future pools,
/// permissions, or replicas.
pub fn connect_readonly(database_url: &str) -> anyhow::Result<Client> {
    let mut client = gobby_core::postgres::connect_readonly(database_url)?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

pub fn read_config_value(conn: &mut Client, key: &str) -> anyhow::Result<Option<String>> {
    gobby_core::postgres::read_config_value(conn, key)
}

#[derive(Debug, Clone)]
pub struct GraphFileFacts {
    pub file_path: String,
    pub imports: Vec<ImportRelation>,
    pub definitions: Vec<Symbol>,
    pub calls: Vec<CallRelation>,
}

pub fn list_indexed_file_paths(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<String>> {
    let rows = conn.query(
        "SELECT file_path FROM code_indexed_files WHERE project_id = $1 ORDER BY file_path",
        &[&project_id],
    )?;
    rows.into_iter()
        .map(|row| row.try_get("file_path").map_err(Into::into))
        .collect()
}

pub fn indexed_project_exists(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<bool> {
    Ok(conn
        .query_opt(
            "SELECT 1 FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )?
        .is_some())
}

pub fn read_graph_file_facts(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<GraphFileFacts> {
    let imports = read_imports_for_file(conn, project_id, file_path)?;
    let definitions = read_symbols_for_file(conn, project_id, file_path)?;
    let calls = read_calls_for_file(conn, project_id, file_path)?;

    Ok(GraphFileFacts {
        file_path: file_path.to_string(),
        imports,
        definitions,
        calls,
    })
}

pub fn indexed_file_exists(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    Ok(conn
        .query_opt(
            "SELECT 1 FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )?
        .is_some())
}

pub fn mark_graph_sync_attempted(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let updated = conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NOW()
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn mark_graph_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let updated = conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = true, graph_sync_attempted_at = NOW()
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn reset_graph_sync_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    Ok(conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NULL
         WHERE project_id = $1",
        &[&project_id],
    )?)
}

pub fn mark_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let updated = conn.execute(
        "UPDATE code_indexed_files
         SET vectors_synced = true
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn mark_project_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    Ok(conn.execute(
        "UPDATE code_indexed_files
         SET vectors_synced = true
         WHERE project_id = $1",
        &[&project_id],
    )?)
}

pub fn file_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let synced = conn
        .query_opt(
            "SELECT vectors_synced
             FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )?
        .map(|row| row.try_get::<_, bool>("vectors_synced"))
        .transpose()?
        .unwrap_or(false);
    Ok(synced)
}

pub fn reset_vectors_sync_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    Ok(conn.execute(
        "UPDATE code_indexed_files
         SET vectors_synced = false
         WHERE project_id = $1",
        &[&project_id],
    )?)
}

fn read_imports_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<ImportRelation>> {
    let rows = conn.query(
        "SELECT source_file, target_module
         FROM code_imports
         WHERE project_id = $1 AND source_file = $2
         ORDER BY target_module",
        &[&project_id, &file_path],
    )?;
    rows.into_iter()
        .map(|row| {
            Ok(ImportRelation {
                file_path: row.try_get("source_file")?,
                module_name: row.try_get("target_module")?,
            })
        })
        .collect()
}

fn read_symbols_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let query = format!(
        "SELECT {} FROM code_symbols s
         WHERE s.project_id = $1 AND s.file_path = $2
         ORDER BY s.line_start, s.byte_start",
        symbol_select_columns("s")
    );
    let rows = conn.query(&query, &[&project_id, &file_path])?;
    rows.iter().map(Symbol::from_row).collect()
}

fn read_calls_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<CallRelation>> {
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, line::BIGINT AS line
         FROM code_calls
         WHERE project_id = $1 AND file_path = $2
         ORDER BY line, caller_symbol_id, callee_name",
        &[&project_id, &file_path],
    )?;
    rows.into_iter()
        .map(|row| {
            let target_kind: String = row.try_get("callee_target_kind")?;
            let callee_symbol_id: String = row.try_get("callee_symbol_id")?;
            let callee_external_module: String = row.try_get("callee_external_module")?;
            Ok(CallRelation {
                caller_symbol_id: row.try_get("caller_symbol_id")?,
                callee_symbol_id: non_empty(callee_symbol_id),
                callee_name: row.try_get("callee_name")?,
                callee_target_kind: call_target_kind_from_str(&target_kind)?,
                callee_external_module: non_empty(callee_external_module),
                file_path: row.try_get("file_path")?,
                line: i64_to_usize(row.try_get("line")?, "line")?,
            })
        })
        .collect()
}

fn non_empty(value: String) -> Option<String> {
    if value.is_empty() { None } else { Some(value) }
}

fn call_target_kind_from_str(value: &str) -> anyhow::Result<CallTargetKind> {
    match value {
        "symbol" => Ok(CallTargetKind::Symbol),
        "unresolved" => Ok(CallTargetKind::Unresolved),
        "external" => Ok(CallTargetKind::External),
        other => bail!("unknown code_calls.callee_target_kind `{other}`"),
    }
}

fn i64_to_usize(value: i64, column: &str) -> anyhow::Result<usize> {
    value
        .try_into()
        .with_context(|| format!("column `{column}` contains negative or too-large value {value}"))
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read as _, Write as _};
    use std::net::TcpListener;
    use std::thread;

    fn bootstrap(hub_backend: &str, database_url: Option<&str>) -> BootstrapDatabase {
        BootstrapDatabase {
            hub_backend: hub_backend.to_string(),
            database_url: database_url.map(str::to_string),
        }
    }

    #[test]
    fn database_url_env_prefers_gcode_specific_var() {
        let resolved = resolve_database_url_from_env(|name| match name {
            GCODE_DATABASE_URL_ENV => Some(" postgresql://env/db ".to_string()),
            GOBBY_POSTGRES_DSN_ENV => Some("postgresql://gobby/db".to_string()),
            _ => None,
        });

        assert_eq!(resolved.as_deref(), Some("postgresql://env/db"));
    }

    #[test]
    fn database_url_env_falls_back_to_gobby_postgres_dsn() {
        let resolved = resolve_database_url_from_env(|name| match name {
            GOBBY_POSTGRES_DSN_ENV => Some(" postgresql://gobby/db ".to_string()),
            _ => None,
        });

        assert_eq!(resolved.as_deref(), Some("postgresql://gobby/db"));
    }

    #[test]
    fn database_url_env_ignores_empty_values() {
        let resolved = resolve_database_url_from_env(|name| match name {
            GCODE_DATABASE_URL_ENV => Some("  ".to_string()),
            GOBBY_POSTGRES_DSN_ENV => Some("\n\t".to_string()),
            _ => None,
        });

        assert_eq!(resolved, None);
    }

    #[test]
    fn database_url_sources_prefer_env_over_daemon_broker() {
        let home = tempfile::tempdir().expect("temp home");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            |_| Ok("postgresql://broker/db".to_string()),
            |name| match name {
                GCODE_DATABASE_URL_ENV => Some("postgresql://env/db".to_string()),
                _ => None,
            },
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://env/db");
    }

    #[test]
    fn database_url_sources_use_daemon_broker_after_env() {
        let home = tempfile::tempdir().expect("temp home");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            |_| Ok("postgresql://broker/db".to_string()),
            |_| None,
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://broker/db");
    }

    #[test]
    fn database_url_sources_fall_back_to_bootstrap_inline_when_daemon_is_unavailable() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join("bootstrap.yaml"),
            "hub_backend: postgres\ndatabase_url: postgresql://inline/db\n",
        )
        .expect("write bootstrap");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            |_| bail!("daemon unavailable"),
            |_| None,
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn database_url_sources_fall_back_to_gcore_before_legacy_gcode_config() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://gcore/db\n",
        )
        .expect("write gcore config");
        std::fs::write(
            home.path().join(GCODE_CONFIG_FILENAME),
            "database_url: postgresql://legacy/db\n",
        )
        .expect("write legacy config");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            |_| bail!("daemon unavailable"),
            |_| None,
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://gcore/db");
    }

    #[test]
    fn adopted_hub_resolves_without_conflict() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://adopted/gobby\n",
        )
        .expect("write gcore config");

        let resolved = resolve_database_url_from_sources_with_identity(
            home.path(),
            |_| Ok("postgresql://adopted/gobby".to_string()),
            |_| None,
            |_| true,
            |_| {
                Ok(gobby_core::provisioning::HubIdentityProbeResult::Known(
                    gobby_core::provisioning::HubIdentity {
                        system_identifier: "cluster-a".to_string(),
                        database_name: "gobby".to_string(),
                    },
                ))
            },
        )
        .expect("resolve adopted hub");

        assert_eq!(resolved, "postgresql://adopted/gobby");
    }

    #[test]
    fn gcode_config_accepts_database_url() {
        let home = tempfile::tempdir().expect("temp home");
        let path = home.path().join(GCODE_CONFIG_FILENAME);
        std::fs::write(&path, "database_url: postgresql://config/db\n").expect("write config");

        let resolved = resolve_database_url_from_config_file(&path)
            .expect("config parses")
            .expect("database_url present");

        assert_eq!(resolved, "postgresql://config/db");
    }

    #[test]
    fn gcode_config_missing_file_is_not_an_override() {
        let home = tempfile::tempdir().expect("temp home");
        let path = home.path().join(GCODE_CONFIG_FILENAME);

        let resolved = resolve_database_url_from_config_file(&path).expect("missing config ok");

        assert_eq!(resolved, None);
    }

    #[test]
    fn gcode_config_empty_file_is_not_an_override() {
        let home = tempfile::tempdir().expect("temp home");
        let path = home.path().join(GCODE_CONFIG_FILENAME);
        std::fs::write(&path, "").expect("write config");

        let resolved = resolve_database_url_from_config_file(&path).expect("empty config ok");

        assert_eq!(resolved, None);
    }

    #[test]
    fn postgres_bootstrap_accepts_inline_url() {
        let resolved = resolve_database_url_from_bootstrap(&bootstrap(
            "postgres",
            Some("postgresql://inline/db"),
        ))
        .expect("resolve inline url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn postgres_bootstrap_rejects_database_url_ref() {
        let err = parse_bootstrap_database(
            "hub_backend: postgres\n\
             database_url_ref: deprecated\n",
        )
        .expect_err("database_url_ref must fail");

        let message = err.to_string();
        assert!(message.contains("database_url_ref is no longer supported"));
        assert!(message.contains("database_url"));
    }

    #[test]
    fn postgres_bootstrap_rejects_database_url_ref_even_with_inline_url() {
        let err = parse_bootstrap_database(
            "hub_backend: postgres\n\
             database_url: postgresql://inline/db\n\
             database_url_ref: deprecated\n",
        )
        .expect_err("database_url_ref must fail");

        assert!(
            err.to_string()
                .contains("database_url_ref is no longer supported")
        );
    }

    #[test]
    fn non_postgres_bootstrap_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(&bootstrap("legacy-local", None))
            .expect_err("non-postgres backend must fail");

        let message = err.to_string();
        assert!(message.contains("hub_backend: postgres"));
        assert!(message.contains("legacy-local"));
    }

    #[test]
    fn missing_hub_backend_fails_clearly() {
        let err = parse_bootstrap_database("database_url: postgresql://inline/db\n")
            .expect_err("missing hub_backend must fail");

        assert!(err.to_string().contains("hub_backend: postgres"));
    }

    #[test]
    fn missing_postgres_dsn_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(&bootstrap("postgres", None))
            .expect_err("missing dsn must fail");

        assert!(err.to_string().contains("database_url"));
    }

    #[test]
    fn parse_bootstrap_database_reads_postgres_fields() {
        let parsed = parse_bootstrap_database(
            "hub_backend: postgres\n\
             database_url: postgresql://inline/db\n",
        )
        .expect("parse bootstrap");

        assert_eq!(parsed.hub_backend, "postgres");
        assert_eq!(
            parsed.database_url.as_deref(),
            Some("postgresql://inline/db")
        );
    }

    #[test]
    fn broker_request_returns_database_url_and_sends_local_token() {
        let (daemon_url, request) = spawn_http_response(http_response(
            "200 OK",
            r#"{"database_url":"postgresql://broker/db"}"#,
        ));

        let resolved =
            request_broker_database_url(&daemon_url, "token-123").expect("broker resolves");
        let request = request.join().expect("read request");

        assert_eq!(resolved, "postgresql://broker/db");
        assert!(request.starts_with("POST /api/local/runtime/database-url HTTP/1.1"));
        assert!(
            request
                .to_ascii_lowercase()
                .contains("x-gobby-local-token: token-123")
        );
    }

    #[test]
    fn broker_request_allows_cold_daemon_latency() {
        let (daemon_url, request) = spawn_http_response_after(
            http_response("200 OK", r#"{"database_url":"postgresql://broker/db"}"#),
            Duration::from_millis(1100),
        );

        let resolved =
            request_broker_database_url(&daemon_url, "token-123").expect("broker resolves");
        let _ = request.join().expect("read request");

        assert_eq!(resolved, "postgresql://broker/db");
    }

    #[test]
    fn broker_missing_token_fails() {
        let home = tempfile::tempdir().expect("temp home");
        let bootstrap_path = write_bootstrap(home.path(), 60887);

        let err = resolve_brokered_database_url_at(home.path(), &bootstrap_path)
            .expect_err("missing token must fail");

        assert!(err.to_string().contains("missing local CLI token"));
    }

    #[test]
    fn broker_daemon_down_fails() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(home.path().join(LOCAL_CLI_TOKEN_FILENAME), "token\n").expect("write token");
        let bootstrap_path = write_bootstrap(home.path(), 9);

        let err = resolve_brokered_database_url_at(home.path(), &bootstrap_path)
            .expect_err("daemon down must fail");

        assert!(
            err.to_string()
                .contains("database_url broker request failed")
        );
    }

    #[test]
    fn broker_auth_failure_fails() {
        let (daemon_url, request) = spawn_http_response(http_response(
            "401 Unauthorized",
            r#"{"error":"bad token"}"#,
        ));

        let err = request_broker_database_url(&daemon_url, "bad-token")
            .expect_err("auth failure must fail");
        let _ = request.join().expect("read request");

        assert!(
            err.to_string()
                .contains("database_url broker request failed")
        );
    }

    #[test]
    fn broker_non_success_status_fails() {
        let (daemon_url, request) = spawn_http_response(http_response(
            "503 Service Unavailable",
            r#"{"error":"unavailable"}"#,
        ));

        let err = request_broker_database_url(&daemon_url, "token")
            .expect_err("non-success status must fail");
        let _ = request.join().expect("read request");

        assert!(
            err.to_string()
                .contains("database_url broker request failed")
        );
    }

    #[test]
    fn broker_invalid_json_fails() {
        let (daemon_url, request) = spawn_http_response(http_response("200 OK", "not json"));

        let err =
            request_broker_database_url(&daemon_url, "token").expect_err("invalid JSON must fail");
        let _ = request.join().expect("read request");

        assert!(
            err.to_string()
                .contains("database_url broker response was not valid JSON")
        );
    }

    #[test]
    fn broker_empty_database_url_fails() {
        let (daemon_url, request) =
            spawn_http_response(http_response("200 OK", r#"{"database_url":"  "}"#));

        let err =
            request_broker_database_url(&daemon_url, "token").expect_err("empty DSN must fail");
        let _ = request.join().expect("read request");

        assert!(
            err.to_string()
                .contains("database_url broker response was empty")
        );
    }

    fn write_bootstrap(home: &Path, daemon_port: u16) -> PathBuf {
        let path = home.join("bootstrap.yaml");
        std::fs::write(
            &path,
            format!("hub_backend: postgres\ndaemon_port: {daemon_port}\nbind_host: 127.0.0.1\n"),
        )
        .expect("write bootstrap");
        path
    }

    fn http_response(status: &str, body: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    fn spawn_http_response(response: String) -> (String, thread::JoinHandle<String>) {
        spawn_http_response_after(response, Duration::ZERO)
    }

    fn spawn_http_response_after(
        response: String,
        delay: Duration,
    ) -> (String, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let addr = listener.local_addr().expect("local addr");
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept request");
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let read = stream.read(&mut buffer).expect("read request");
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            thread::sleep(delay);
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            String::from_utf8_lossy(&request).into_owned()
        });
        (format!("http://{addr}"), handle)
    }
}
