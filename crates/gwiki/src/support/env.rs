// A 500 MB ceiling keeps video/audio/PDF inbox imports usable while preventing
// accidental multi-GB reads from exhausting memory before media-specific
// ingestion can stream or degrade the file.
const DEFAULT_MAX_INBOX_ITEM_BYTES: u64 = 500_000_000;

use crate::error::WikiError;
use anyhow::{Context, anyhow, bail};
use gobby_core::provisioning::{StandaloneConfig, gcore_config_path};
use serde::Deserialize;
use std::net::{SocketAddr, ToSocketAddrs};
use std::path::Path;
use std::time::Duration;

const GWIKI_DATABASE_URL_ENV: &str = "GWIKI_DATABASE_URL";
const GOBBY_POSTGRES_DSN_ENV: &str = "GOBBY_POSTGRES_DSN";
const GWIKI_BROKER_TIMEOUT_MS_ENV: &str = "GWIKI_BROKER_TIMEOUT_MS";
const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
const DEFAULT_BROKER_TIMEOUT: Duration = Duration::from_millis(7000);

#[derive(Deserialize)]
struct HubBootstrap {
    hub_backend: Option<String>,
    database_url: Option<String>,
}

#[derive(Debug)]
struct ValidatedDaemonUrl {
    request_base_url: String,
    host_header: String,
}

pub(crate) fn database_url() -> anyhow::Result<Option<String>> {
    if let Some(database_url) = database_url_from_env() {
        return Ok(Some(database_url));
    }

    let home = gobby_core::gobby_home()?;
    let bootstrap_path = home.join("bootstrap.yaml");
    match resolve_brokered_database_url_at(&home, &bootstrap_path) {
        Ok(database_url) => return Ok(Some(database_url)),
        Err(error) => {
            log::debug!("failed to resolve brokered gwiki database URL: {error}");
        }
    }
    if let Some(database_url) = resolve_database_url_from_bootstrap_file(&bootstrap_path)? {
        return Ok(Some(database_url));
    }
    resolve_database_url_from_gcore_config(&home)
}

pub(crate) fn database_url_for(command: &str) -> Result<Option<String>, WikiError> {
    database_url().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve PostgreSQL hub for {command}: {error}"),
    })
}

pub(crate) fn database_url_from_env() -> Option<String> {
    [GWIKI_DATABASE_URL_ENV, GOBBY_POSTGRES_DSN_ENV]
        .into_iter()
        .find_map(|name| {
            std::env::var(name)
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
}

fn resolve_database_url_from_gcore_config(home: &Path) -> anyhow::Result<Option<String>> {
    let Some(config) = StandaloneConfig::read_at(&gcore_config_path(home))? else {
        return Ok(None);
    };
    Ok(config
        .get("databases.postgres.dsn")
        .and_then(|value| non_empty_trimmed(Some(value.to_string()))))
}

fn resolve_database_url_from_bootstrap_file(path: &Path) -> anyhow::Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
    }
    let contents = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read Gobby bootstrap at {}", path.display()))?;
    let bootstrap: HubBootstrap = serde_yaml::from_str(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    if matches!(bootstrap.hub_backend.as_deref(), Some(backend) if backend != "postgres") {
        return Ok(None);
    }
    Ok(non_empty_trimmed(bootstrap.database_url))
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
    let daemon = validate_loopback_daemon_url(daemon_url)?;
    let url = format!(
        "{}/api/local/runtime/database-url",
        daemon.request_base_url.trim_end_matches('/')
    );
    let timeout = broker_timeout();
    let agent = ureq::AgentBuilder::new().timeout(timeout).build();
    let response = agent
        .post(&url)
        .set("Host", &daemon.host_header)
        .set("X-Gobby-Local-Token", token)
        .call()
        .map_err(|err| {
            anyhow!(
                "database_url broker request failed after {}ms: {err}",
                timeout.as_millis()
            )
        })?;
    let body = response
        .into_string()
        .context("database_url broker response body was not valid UTF-8")?;
    let body: serde_json::Value =
        serde_json::from_str(&body).context("database_url broker response was not valid JSON")?;
    let database_url = body
        .get("database_url")
        .and_then(|value| value.as_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow!("database_url broker response omitted database_url"))?;
    validate_database_url(database_url)
}

fn broker_timeout() -> Duration {
    let Some(raw) = std::env::var(GWIKI_BROKER_TIMEOUT_MS_ENV).ok() else {
        return DEFAULT_BROKER_TIMEOUT;
    };
    raw.trim()
        .parse::<u64>()
        .ok()
        .filter(|millis| *millis > 0)
        .map(Duration::from_millis)
        .unwrap_or(DEFAULT_BROKER_TIMEOUT)
}

fn validate_loopback_daemon_url(daemon_url: &str) -> anyhow::Result<ValidatedDaemonUrl> {
    let url = url::Url::parse(daemon_url)
        .with_context(|| format!("database_url broker daemon URL is invalid: {daemon_url}"))?;
    let host = url
        .host_str()
        .ok_or_else(|| anyhow!("database_url broker daemon URL must include a host"))?;
    let port = url.port_or_known_default().ok_or_else(|| {
        anyhow!("database_url broker daemon URL must include a port or known scheme")
    })?;
    let resolved = (host, port)
        .to_socket_addrs()
        .with_context(|| format!("resolve database_url broker daemon host `{host}`"))?
        .collect::<Vec<_>>();
    if resolved.is_empty() {
        bail!("database_url broker daemon host `{host}` resolved no addresses");
    }
    if resolved.iter().any(|addr| !addr.ip().is_loopback()) {
        bail!("database_url broker daemon host `{host}` must resolve only to loopback addresses");
    }
    let target_addr = resolved[0];
    Ok(ValidatedDaemonUrl {
        request_base_url: request_base_url(&url, target_addr)?,
        host_header: host_header(host, url.port()),
    })
}

fn request_base_url(url: &url::Url, target_addr: SocketAddr) -> anyhow::Result<String> {
    let mut request_url = url::Url::parse(&format!("{}://{}", url.scheme(), target_addr))
        .context("construct database_url broker request URL")?;
    request_url.set_path(url.path());
    request_url.set_query(url.query());
    Ok(request_url.to_string())
}

fn host_header(host: &str, port: Option<u16>) -> String {
    let host = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    };
    match port {
        Some(port) => format!("{host}:{port}"),
        None => host,
    }
}

fn validate_database_url(database_url: &str) -> anyhow::Result<String> {
    let parsed = url::Url::parse(database_url)
        .with_context(|| "database_url broker returned an invalid PostgreSQL URL")?;
    if !matches!(parsed.scheme(), "postgres" | "postgresql") {
        bail!(
            "database_url broker returned unsupported scheme `{}`",
            parsed.scheme()
        );
    }
    if parsed.host_str().is_none() {
        bail!("database_url broker returned a PostgreSQL URL without a host");
    }
    if parsed.path().trim_matches('/').is_empty() {
        bail!("database_url broker returned a PostgreSQL URL without a database name");
    }
    Ok(database_url.trim().to_string())
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

pub(crate) fn max_inbox_item_bytes_from_env() -> u64 {
    match std::env::var("GWIKI_MAX_INBOX_ITEM_BYTES") {
        Ok(raw) => parse_positive_u64(&raw).unwrap_or_else(|| {
            eprintln!("warning: ignoring invalid GWIKI_MAX_INBOX_ITEM_BYTES={raw}");
            DEFAULT_MAX_INBOX_ITEM_BYTES
        }),
        Err(_) => DEFAULT_MAX_INBOX_ITEM_BYTES,
    }
}

fn parse_positive_u64(raw: &str) -> Option<u64> {
    raw.trim().parse::<u64>().ok().filter(|value| *value > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    use crate::support::test_env::EnvGuard;

    #[test]
    fn positive_u64_env_parser_rejects_invalid_values() {
        assert_eq!(parse_positive_u64("42"), Some(42));
        assert_eq!(parse_positive_u64(" 7 "), Some(7));
        assert_eq!(parse_positive_u64("0"), None);
        assert_eq!(parse_positive_u64("-1"), None);
        assert_eq!(parse_positive_u64("nope"), None);
    }

    #[test]
    #[serial_test::serial]
    fn database_url_uses_gobby_broker_when_env_missing() {
        let expected_database_url = "postgresql://brokered.example/gobby";
        let token = "local-token";
        let (port, handle) = spawn_database_url_broker(expected_database_url, token);
        let home = tempfile::tempdir().expect("create home");
        fs::write(
            home.path().join("bootstrap.yaml"),
            format!("daemon_port: {port}\nbind_host: 127.0.0.1\n"),
        )
        .expect("write bootstrap");
        fs::write(home.path().join("local_cli_token"), format!("{token}\n")).expect("write token");
        let _env = EnvGuard::set("GOBBY_HOME", home.path().as_os_str())
            .and_unset("GWIKI_DATABASE_URL")
            .and_unset("GOBBY_POSTGRES_DSN");

        let resolved = database_url()
            .expect("resolve database url")
            .expect("brokered database url");

        assert_eq!(resolved, expected_database_url);
        let request = handle.join().expect("broker thread");
        assert!(request.starts_with("POST /api/local/runtime/database-url "));
        assert!(request.contains(&format!("Host: 127.0.0.1:{port}")));
        assert!(request.contains("X-Gobby-Local-Token: local-token"));
    }

    #[test]
    fn database_url_broker_rejects_non_loopback_daemon_host() {
        let error = validate_loopback_daemon_url("http://192.0.2.1:60887")
            .expect_err("non-loopback daemon host is rejected");

        assert!(
            error
                .to_string()
                .contains("must resolve only to loopback addresses")
        );
    }

    fn spawn_database_url_broker(
        database_url: &'static str,
        token: &'static str,
    ) -> (u16, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind broker");
        let port = listener.local_addr().expect("broker address").port();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept broker request");
            let mut buffer = [0_u8; 4096];
            let bytes = stream.read(&mut buffer).expect("read broker request");
            let request = String::from_utf8_lossy(&buffer[..bytes]).into_owned();
            assert!(request.contains(&format!("X-Gobby-Local-Token: {token}")));
            let body = format!(r#"{{"database_url":"{database_url}"}}"#);
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                body.len(),
                body
            )
            .expect("write broker response");
            request
        });
        (port, handle)
    }
}
