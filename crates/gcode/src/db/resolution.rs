use std::net::ToSocketAddrs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context as _, anyhow, bail};
use gobby_core::provisioning::{GCORE_CONFIG_FILENAME, StandaloneConfig};
use serde::Deserialize;

const GCODE_DATABASE_URL_ENV: &str = "GCODE_DATABASE_URL";
const GOBBY_POSTGRES_DSN_ENV: &str = "GOBBY_POSTGRES_DSN";
const GCODE_BROKER_TIMEOUT_MS_ENV: &str = "GCODE_BROKER_TIMEOUT_MS";
const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
const DEFAULT_BROKER_TIMEOUT: Duration = Duration::from_millis(7000);

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
    gobby_core::gobby_home()
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

    let gcore_database_url = match resolve_database_url_from_gcore_config(home) {
        Ok(database_url) => database_url,
        Err(error) => {
            log::warn!("failed to read gcore config database URL: {error}");
            None
        }
    };

    if let Ok(database_url) = broker_resolver(&path) {
        if let Some(database_url) = resolve_recorded_hub_database_url(
            gcore_database_url.as_deref(),
            &database_url,
            &mut database_reachable,
            &mut identity_probe,
        )? {
            return Ok(database_url);
        }
        return Ok(database_url);
    }

    if let Some(database_url) = resolve_database_url_from_bootstrap_file(&path)? {
        if let Some(database_url) = resolve_recorded_hub_database_url(
            gcore_database_url.as_deref(),
            &database_url,
            &mut database_reachable,
            &mut identity_probe,
        )? {
            return Ok(database_url);
        }
        return Ok(database_url);
    }

    if let Some(database_url) = gcore_database_url {
        return Ok(database_url);
    }

    bail!(
        "missing Gobby PostgreSQL configuration. Run `gcode setup --standalone`, set {GCODE_DATABASE_URL_ENV}, or configure the Gobby daemon bootstrap."
    )
}

fn resolve_recorded_hub_database_url(
    gcore_database_url: Option<&str>,
    candidate_database_url: &str,
    database_reachable: &mut impl FnMut(&str) -> bool,
    identity_probe: &mut impl FnMut(
        &str,
    )
        -> anyhow::Result<gobby_core::provisioning::HubIdentityProbeResult>,
) -> anyhow::Result<Option<String>> {
    Ok(gobby_core::provisioning::resolve_recorded_hub_database_url(
        gcore_database_url,
        Some(candidate_database_url),
        database_reachable,
        identity_probe,
    )?
    .map(|resolution| resolution.database_url))
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
    validate_loopback_daemon_url(daemon_url)?;
    let url = format!(
        "{}/api/local/runtime/database-url",
        daemon_url.trim_end_matches('/')
    );
    let timeout = broker_timeout();
    let agent = ureq::AgentBuilder::new().timeout(timeout).build();
    let response = agent
        .post(&url)
        .set("X-Gobby-Local-Token", token)
        .call()
        .map_err(|err| {
            anyhow!(
                "database_url broker request failed after {}ms: {err}",
                timeout.as_millis()
            )
        })?;
    let body: BrokerDatabaseUrlResponse = response
        .into_json()
        .context("database_url broker response was not valid JSON")?;
    let database_url = body.database_url.trim().to_string();
    validate_broker_database_url(&database_url)
}

fn broker_timeout() -> Duration {
    broker_timeout_from_env(|name| std::env::var(name).ok())
}

fn broker_timeout_from_env(env: impl Fn(&str) -> Option<String>) -> Duration {
    let Some(raw) = env(GCODE_BROKER_TIMEOUT_MS_ENV) else {
        return DEFAULT_BROKER_TIMEOUT;
    };
    match raw.trim().parse::<u64>() {
        Ok(value) if value > 0 => Duration::from_millis(value),
        _ => {
            log::warn!(
                "invalid {GCODE_BROKER_TIMEOUT_MS_ENV}={raw:?}; using default {}ms",
                DEFAULT_BROKER_TIMEOUT.as_millis()
            );
            DEFAULT_BROKER_TIMEOUT
        }
    }
}

fn validate_loopback_daemon_url(daemon_url: &str) -> anyhow::Result<()> {
    let url = reqwest::Url::parse(daemon_url)
        .with_context(|| format!("database_url broker daemon URL is invalid: {daemon_url}"))?;
    let host = url
        .host_str()
        .ok_or_else(|| anyhow!("database_url broker daemon URL must include a host"))?;
    let port = url.port_or_known_default().ok_or_else(|| {
        anyhow!("database_url broker daemon URL must include a port or known scheme")
    })?;
    let mut resolved = (host, port)
        .to_socket_addrs()
        .with_context(|| format!("resolve database_url broker daemon host `{host}`"))?
        .peekable();
    if resolved.peek().is_none() {
        bail!("database_url broker daemon host `{host}` resolved no addresses");
    }
    if resolved.all(|addr| addr.ip().is_loopback()) {
        Ok(())
    } else {
        bail!("database_url broker daemon host `{host}` must resolve only to loopback addresses");
    }
}

fn validate_broker_database_url(database_url: &str) -> anyhow::Result<String> {
    if database_url.is_empty() {
        bail!("database_url broker response was empty");
    }
    let Some(without_scheme) = database_url
        .strip_prefix("postgres://")
        .or_else(|| database_url.strip_prefix("postgresql://"))
    else {
        bail!("database_url broker response must use postgres:// or postgresql://");
    };
    let Some((authority, path_and_query)) = without_scheme.split_once('/') else {
        bail!("database_url broker response must include a database path");
    };
    let host_port = authority.rsplit('@').next().unwrap_or_default();
    let has_host = if let Some(rest) = host_port.strip_prefix('[') {
        rest.split_once(']')
            .is_some_and(|(host, _)| !host.is_empty())
    } else {
        !host_port.split(':').next().unwrap_or_default().is_empty()
    };
    if !has_host {
        bail!("database_url broker response must include a host");
    }
    let database_path = path_and_query.split('?').next().unwrap_or_default();
    if database_path.is_empty() {
        bail!("database_url broker response must include a database path");
    }
    Ok(database_url.to_string())
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
    fn database_url_sources_use_gcore_after_daemon_and_bootstrap() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://gcore/db\n",
        )
        .expect("write gcore config");

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
    fn postgres_bootstrap_accepts_inline_url() {
        let resolved = resolve_database_url_from_bootstrap(&bootstrap(
            "postgres",
            Some("postgresql://inline/db"),
        ))
        .expect("resolve inline url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn non_postgres_bootstrap_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(&bootstrap("local-file", None))
            .expect_err("non-postgres backend must fail");

        let message = err.to_string();
        assert!(message.contains("hub_backend: postgres"));
        assert!(message.contains("local-file"));
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
    fn broker_request_rejects_non_loopback_daemon_url_before_sending_local_token() {
        let err = request_broker_database_url("http://192.0.2.1:60887", "token-123")
            .expect_err("non-loopback daemon URL must fail");

        assert!(
            err.to_string()
                .contains("must resolve only to loopback addresses")
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
    fn broker_timeout_defaults_to_seven_seconds() {
        let timeout = broker_timeout_from_env(|_| None);

        assert_eq!(timeout, Duration::from_millis(7000));
    }

    #[test]
    fn broker_timeout_reads_positive_env_value() {
        let timeout = broker_timeout_from_env(|name| {
            (name == GCODE_BROKER_TIMEOUT_MS_ENV).then(|| "1250".to_string())
        });

        assert_eq!(timeout, Duration::from_millis(1250));
    }

    #[test]
    fn broker_timeout_ignores_invalid_env_value() {
        let timeout = broker_timeout_from_env(|name| {
            (name == GCODE_BROKER_TIMEOUT_MS_ENV).then(|| "0".to_string())
        });

        assert_eq!(timeout, DEFAULT_BROKER_TIMEOUT);
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

    #[test]
    fn broker_invalid_database_url_scheme_fails() {
        let err = validate_broker_database_url("http://broker/db")
            .expect_err("non-postgres scheme must fail");

        assert!(
            err.to_string()
                .contains("must use postgres:// or postgresql://")
        );
    }

    #[test]
    fn broker_missing_database_url_host_fails() {
        let err =
            validate_broker_database_url("postgresql:///db").expect_err("missing host must fail");

        assert!(
            err.to_string()
                .contains("database_url broker response must include a host")
        );
    }

    #[test]
    fn broker_missing_database_url_path_fails() {
        let err = validate_broker_database_url("postgresql://broker/")
            .expect_err("missing path must fail");

        assert!(
            err.to_string()
                .contains("database_url broker response must include a database path")
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
