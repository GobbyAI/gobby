//! Daemon URL resolution: one env-override contract plus unroutable-host
//! normalization, shared by every Gobby binary.
//!
//! Resolution order for [`daemon_url`]:
//! 1. `GOBBY_DAEMON_URL` — full base-URL override (trailing slashes are
//!    trimmed). Empty values are ignored.
//! 2. `GOBBY_PORT` — port-only override, dialed on loopback. Empty or
//!    unparseable values are ignored.
//! 3. `bootstrap.yaml` — the daemon's advertised endpoint, with dial
//!    normalization applied.
//!
//! The Gobby daemon advertises a listen address in `bootstrap.yaml`, but
//! clients need a *dial* address. `0.0.0.0` and `::` are wildcard listen
//! addresses — you cannot `connect(2)` to them — so a user who sets
//! `bind_host: 0.0.0.0` (to expose the daemon on their LAN) must still
//! dial `127.0.0.1` from a local client. This module applies that
//! normalization uniformly; [`crate::bootstrap`] returns the raw endpoint.

use std::borrow::Cow;
use std::path::Path;

use crate::bootstrap::{DaemonEndpoint, read_daemon_endpoint, read_daemon_endpoint_at};

/// Compose the daemon dial URL from env overrides and the default bootstrap path.
///
/// This is the resolver all Gobby binaries share: `GOBBY_DAEMON_URL` wins,
/// then `GOBBY_PORT`, then the `bootstrap.yaml` endpoint.
pub fn daemon_url() -> String {
    env_override(
        std::env::var("GOBBY_DAEMON_URL").ok().as_deref(),
        std::env::var("GOBBY_PORT").ok().as_deref(),
    )
    .unwrap_or_else(|| endpoint_to_url(&read_daemon_endpoint()))
}

/// Compose the daemon dial URL from a specific bootstrap file path.
///
/// Env overrides do not apply — an explicit path is already an override.
/// Exposed for tests and for callers that know the path explicitly.
pub fn daemon_url_at(path: &Path) -> String {
    endpoint_to_url(&read_daemon_endpoint_at(path))
}

/// Apply the env-override contract: a non-empty `GOBBY_DAEMON_URL` beats a
/// parseable `GOBBY_PORT`. Ignoring empty/garbage values keeps a stray
/// `GOBBY_PORT=""` from masking the bootstrap.yaml endpoint.
fn env_override(url: Option<&str>, port: Option<&str>) -> Option<String> {
    if let Some(url) = url {
        let url = url.trim();
        if !url.is_empty() {
            return Some(url.trim_end_matches('/').to_string());
        }
    }
    port?
        .trim()
        .parse::<u16>()
        .ok()
        .map(|port| format!("http://127.0.0.1:{port}"))
}

fn endpoint_to_url(endpoint: &DaemonEndpoint) -> String {
    let host = dial_host(&endpoint.host);
    format!("http://{host}:{}", endpoint.port)
}

/// Map a listen host to a dial host.
///
/// Wildcard listen addresses (`0.0.0.0`, `::`, `::0`, `[::]`) and empty
/// hosts are rewritten to `127.0.0.1`. Bare IPv6 literals are bracketed
/// for URL embedding. Everything else passes through unchanged —
/// hostnames, named interfaces, explicit IPv4 literals.
fn dial_host(host: &str) -> Cow<'_, str> {
    match host.trim() {
        "" | "0.0.0.0" | "::" | "::0" | "[::]" => Cow::Borrowed("127.0.0.1"),
        host if host.contains(':') && !host.starts_with('[') => Cow::Owned(format!("[{host}]")),
        host => Cow::Borrowed(host),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    fn write_bootstrap(contents: &str) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, contents).unwrap();
        (dir, path)
    }

    #[test]
    fn default_url_when_file_missing() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("nope.yaml");
        assert_eq!(daemon_url_at(&path), "http://127.0.0.1:60887");
    }

    #[test]
    fn wildcard_ipv4_normalizes_to_loopback() {
        let (_dir, path) = write_bootstrap("daemon_port: 60887\nbind_host: 0.0.0.0\n");
        assert_eq!(daemon_url_at(&path), "http://127.0.0.1:60887");
    }

    #[test]
    fn wildcard_ipv6_normalizes_to_loopback() {
        let (_dir, path) = write_bootstrap(
            r#"daemon_port: 60887
bind_host: "::"
"#,
        );
        assert_eq!(daemon_url_at(&path), "http://127.0.0.1:60887");
    }

    #[test]
    fn wildcard_ipv6_zero_normalizes_to_loopback() {
        let (_dir, path) = write_bootstrap(
            r#"daemon_port: 60887
bind_host: "::0"
"#,
        );
        assert_eq!(daemon_url_at(&path), "http://127.0.0.1:60887");
    }

    #[test]
    fn localhost_passes_through() {
        let (_dir, path) = write_bootstrap("daemon_port: 60887\nbind_host: localhost\n");
        assert_eq!(daemon_url_at(&path), "http://localhost:60887");
    }

    #[test]
    fn custom_port_and_host_compose() {
        let (_dir, path) = write_bootstrap("daemon_port: 61234\nbind_host: 10.0.0.5\n");
        assert_eq!(daemon_url_at(&path), "http://10.0.0.5:61234");
    }

    #[test]
    fn bare_ipv6_literal_is_bracketed() {
        let (_dir, path) = write_bootstrap(
            r#"daemon_port: 61234
bind_host: "::1"
"#,
        );
        assert_eq!(daemon_url_at(&path), "http://[::1]:61234");
    }

    #[test]
    fn bracketed_ipv6_literal_passes_through() {
        let (_dir, path) = write_bootstrap(
            r#"daemon_port: 61234
bind_host: "[::1]"
"#,
        );
        assert_eq!(daemon_url_at(&path), "http://[::1]:61234");
    }

    #[test]
    fn env_override_url_beats_port() {
        assert_eq!(
            env_override(Some("http://override.invalid:1234"), Some("61999")),
            Some("http://override.invalid:1234".to_string())
        );
    }

    #[test]
    fn env_override_url_trims_trailing_slashes() {
        assert_eq!(
            env_override(Some("http://override.invalid:1234/"), None),
            Some("http://override.invalid:1234".to_string())
        );
    }

    #[test]
    fn env_override_empty_url_falls_back_to_port() {
        assert_eq!(
            env_override(Some(""), Some("61999")),
            Some("http://127.0.0.1:61999".to_string())
        );
    }

    #[test]
    fn env_override_ignores_unparseable_or_empty_port() {
        assert_eq!(env_override(None, Some("not-a-port")), None);
        assert_eq!(env_override(None, Some("")), None);
        assert_eq!(env_override(None, Some("70000")), None);
    }

    #[test]
    fn env_override_absent_returns_none() {
        assert_eq!(env_override(None, None), None);
    }

    #[test]
    fn daemon_url_honors_env_contract_over_bootstrap() {
        let _lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let saved: Vec<(&str, Option<std::ffi::OsString>)> =
            ["GOBBY_DAEMON_URL", "GOBBY_PORT", "GOBBY_HOME"]
                .map(|name| (name, std::env::var_os(name)))
                .into();

        let dir = tempdir().unwrap();
        fs::write(dir.path().join("bootstrap.yaml"), "daemon_port: 61111\n").unwrap();
        // SAFETY: env mutation is serialized through TEST_ENV_LOCK and
        // restored before the lock is released.
        unsafe {
            std::env::set_var("GOBBY_HOME", dir.path());
            std::env::set_var("GOBBY_DAEMON_URL", "http://override.invalid:1234/");
            std::env::set_var("GOBBY_PORT", "61999");
        }
        let with_url = daemon_url();
        // SAFETY: as above.
        unsafe { std::env::remove_var("GOBBY_DAEMON_URL") };
        let with_port = daemon_url();
        // SAFETY: as above.
        unsafe { std::env::remove_var("GOBBY_PORT") };
        let from_bootstrap = daemon_url();

        // SAFETY: still holding TEST_ENV_LOCK; restores the original values.
        unsafe {
            for (name, value) in saved {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }

        assert_eq!(with_url, "http://override.invalid:1234");
        assert_eq!(with_port, "http://127.0.0.1:61999");
        assert_eq!(from_bootstrap, "http://127.0.0.1:61111");
    }
}
