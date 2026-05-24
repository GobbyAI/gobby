use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context as _, anyhow, bail};
use postgres::{Client, NoTls};
use serde::Deserialize;

use crate::schema;

const POSTGRES_DATABASE_URL_REF: &str = "keyring:gobby:postgres_database_url";
const DAEMON_POSTGRES_DATABASE_URL_REF: &str = "daemon:gobby:postgres_database_url";
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

/// Resolve the PostgreSQL hub DSN from explicit overrides or Gobby bootstrap config.
///
/// gcode intentionally has no local database fallback. Keyring-backed bootstraps resolve
/// through the long-lived daemon broker so short-lived gcode processes never touch the OS
/// keyring directly.
pub fn resolve_database_url() -> anyhow::Result<String> {
    let home = gobby_home()?;
    resolve_database_url_from_sources(
        &home,
        |bootstrap_path| resolve_brokered_database_url_at(&home, bootstrap_path),
        |name| std::env::var(name).ok(),
    )
}

fn resolve_database_url_from_sources(
    home: &Path,
    broker_resolver: impl Fn(&Path) -> anyhow::Result<String>,
    get_var: impl FnMut(&str) -> Option<String>,
) -> anyhow::Result<String> {
    let path = home.join("bootstrap.yaml");

    if let Ok(database_url) = broker_resolver(&path) {
        return Ok(database_url);
    }

    if let Some(database_url) = resolve_database_url_from_env(get_var) {
        return Ok(database_url);
    }

    if let Some(database_url) =
        resolve_database_url_from_config_file(&home.join(GCODE_CONFIG_FILENAME))?
    {
        return Ok(database_url);
    }

    let contents = std::fs::read_to_string(&path).with_context(|| {
        format!(
            "missing Gobby bootstrap at {}. Configure the Gobby PostgreSQL hub before running gcode.",
            path.display()
        )
    })?;
    let bootstrap = parse_bootstrap_database(&contents)?;
    resolve_database_url_from_bootstrap(&bootstrap, || broker_resolver(&path))
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

    Ok(BootstrapDatabase {
        hub_backend: get_string("hub_backend")?
            .context("bootstrap.yaml must include `hub_backend: postgres`")?,
        database_url: get_string("database_url")?,
        database_url_ref: get_string("database_url_ref")?,
    })
}

fn resolve_database_url_from_bootstrap(
    bootstrap: &BootstrapDatabase,
    broker_resolver: impl Fn() -> anyhow::Result<String>,
) -> anyhow::Result<String> {
    if bootstrap.hub_backend != "postgres" {
        bail!(
            "gcode requires `hub_backend: postgres` in bootstrap.yaml. Current hub_backend is `{}`. Configure the Gobby PostgreSQL hub before running gcode.",
            bootstrap.hub_backend
        );
    }

    if let Some(database_url) = bootstrap.database_url.as_deref() {
        return Ok(database_url.to_string());
    }

    if let Some(database_url_ref) = bootstrap.database_url_ref.as_deref() {
        parse_database_url_ref(database_url_ref)?;
        let database_url = broker_resolver().with_context(|| {
            format!(
                "failed to resolve database_url_ref {database_url_ref} through the local Gobby daemon broker. gcode does not read the OS keyring directly; start the Gobby daemon or use inline database_url in bootstrap.yaml for daemonless/manual setups"
            )
        })?;
        let database_url = database_url.trim().to_string();
        if database_url.is_empty() {
            bail!("database_url broker response for {database_url_ref} was empty");
        }
        return Ok(database_url);
    }

    bail!(
        "hub_backend=postgres requires `database_url_ref: {POSTGRES_DATABASE_URL_REF}`, `database_url_ref: {DAEMON_POSTGRES_DATABASE_URL_REF}`, or an inline `database_url`"
    )
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    let value = value?.trim().to_string();
    if value.is_empty() { None } else { Some(value) }
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

fn parse_database_url_ref(database_url_ref: &str) -> anyhow::Result<()> {
    let parts: Vec<&str> = database_url_ref.splitn(3, ':').collect();
    match parts.as_slice() {
        ["keyring" | "daemon", "gobby", "postgres_database_url"] => Ok(()),
        _ => bail!(
            "database_url_ref must be {POSTGRES_DATABASE_URL_REF} or {DAEMON_POSTGRES_DATABASE_URL_REF}"
        ),
    }
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
    use std::io::{Read as _, Write as _};
    use std::net::TcpListener;
    use std::thread;

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
    fn database_url_sources_prefer_daemon_broker() {
        let home = tempfile::tempdir().expect("temp home");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            |_| Ok("postgresql://broker/db".to_string()),
            |name| match name {
                GCODE_DATABASE_URL_ENV => Some("postgresql://env/db".to_string()),
                _ => None,
            },
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://broker/db");
    }

    #[test]
    fn database_url_sources_fall_back_to_env_when_daemon_is_unavailable() {
        let home = tempfile::tempdir().expect("temp home");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            |_| bail!("daemon unavailable"),
            |name| match name {
                GOBBY_POSTGRES_DSN_ENV => Some("postgresql://env/db".to_string()),
                _ => None,
            },
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://env/db");
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
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://inline/db");
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
    fn postgres_bootstrap_resolves_keyring_ref_through_broker() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(POSTGRES_DATABASE_URL_REF)),
            || Ok("postgresql://broker/db".to_string()),
        )
        .expect("resolve ref");

        assert_eq!(resolved, "postgresql://broker/db");
    }

    #[test]
    fn postgres_bootstrap_resolves_daemon_ref_through_broker() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(DAEMON_POSTGRES_DATABASE_URL_REF)),
            || Ok("postgresql://broker/db".to_string()),
        )
        .expect("resolve ref");

        assert_eq!(resolved, "postgresql://broker/db");
    }

    #[test]
    fn postgres_bootstrap_ref_fails_clearly_when_broker_fails() {
        let err = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(POSTGRES_DATABASE_URL_REF)),
            || bail!("broker unavailable"),
        )
        .expect_err("broker failure must fail");

        let message = err.to_string();
        assert!(message.contains("local Gobby daemon broker"));
        assert!(message.contains("does not read the OS keyring directly"));
    }

    #[test]
    fn postgres_bootstrap_fails_when_broker_token_is_missing() {
        let home = tempfile::tempdir().expect("temp home");
        let bootstrap_path = write_bootstrap(home.path(), 60887);

        let err = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(POSTGRES_DATABASE_URL_REF)),
            || resolve_brokered_database_url_at(home.path(), &bootstrap_path),
        )
        .expect_err("missing broker token must fail");

        assert!(err.to_string().contains("local Gobby daemon broker"));
    }

    #[test]
    fn postgres_bootstrap_fails_when_broker_is_unreachable() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(home.path().join(LOCAL_CLI_TOKEN_FILENAME), "token\n").expect("write token");
        let bootstrap_path = write_bootstrap(home.path(), unused_local_port());

        let err = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(POSTGRES_DATABASE_URL_REF)),
            || resolve_brokered_database_url_at(home.path(), &bootstrap_path),
        )
        .expect_err("unreachable broker must fail");

        assert!(err.to_string().contains("local Gobby daemon broker"));
    }

    #[test]
    fn postgres_bootstrap_fails_after_broker_401() {
        assert_broker_http_failure_fails("401 Unauthorized", r#"{"error":"bad token"}"#);
    }

    #[test]
    fn postgres_bootstrap_fails_after_broker_403() {
        assert_broker_http_failure_fails("403 Forbidden", r#"{"error":"forbidden"}"#);
    }

    #[test]
    fn postgres_bootstrap_fails_after_broker_non_2xx() {
        assert_broker_http_failure_fails("503 Service Unavailable", r#"{"error":"unavailable"}"#);
    }

    #[test]
    fn postgres_bootstrap_fails_after_broker_invalid_json() {
        assert_broker_http_failure_fails("200 OK", "not json");
    }

    #[test]
    fn postgres_bootstrap_fails_after_broker_empty_database_url() {
        assert_broker_http_failure_fails("200 OK", r#"{"database_url":"  "}"#);
    }

    #[test]
    fn postgres_bootstrap_prefers_inline_url_over_ref() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap(
                "postgres",
                Some("postgresql://inline/db"),
                Some(POSTGRES_DATABASE_URL_REF),
            ),
            || unreachable!("broker should not be used when inline url is present"),
        )
        .expect("resolve inline url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn postgres_bootstrap_accepts_inline_url() {
        let resolved = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", Some("postgresql://inline/db"), None),
            || unreachable!("broker should not be used"),
        )
        .expect("resolve inline url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn non_postgres_bootstrap_fails_clearly() {
        let err =
            resolve_database_url_from_bootstrap(&bootstrap("legacy-local", None, None), || {
                unreachable!("broker should not be used")
            })
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
        let err = resolve_database_url_from_bootstrap(&bootstrap("postgres", None, None), || {
            unreachable!("broker should not be used")
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
            || unreachable!("broker should not be used for unsupported refs"),
        )
        .expect_err("unsupported ref must fail");

        assert!(err.to_string().contains(POSTGRES_DATABASE_URL_REF));
    }

    #[test]
    fn parse_bootstrap_database_reads_postgres_fields() {
        let parsed = parse_bootstrap_database(
            "hub_backend: postgres\n\
             database_url_ref: daemon:gobby:postgres_database_url\n",
        )
        .expect("parse bootstrap");

        assert_eq!(parsed.hub_backend, "postgres");
        assert_eq!(
            parsed.database_url_ref.as_deref(),
            Some(DAEMON_POSTGRES_DATABASE_URL_REF)
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

    fn assert_broker_http_failure_fails(status: &str, body: &str) {
        let (daemon_url, request) = spawn_http_response(http_response(status, body));

        let err = resolve_database_url_from_bootstrap(
            &bootstrap("postgres", None, Some(POSTGRES_DATABASE_URL_REF)),
            || request_broker_database_url(&daemon_url, "token"),
        )
        .expect_err("broker failure must fail");
        let _ = request.join().expect("read request");

        assert!(err.to_string().contains("local Gobby daemon broker"));
    }

    fn unused_local_port() -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind unused local port");
        listener.local_addr().expect("local addr").port()
    }
}
