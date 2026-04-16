//! Bootstrap config resolution.
//!
//! Reads `~/.gobby/bootstrap.yaml` to discover how the Gobby daemon is
//! reachable: its TCP port and bind host. Falls back to loopback defaults
//! when the file is missing, unreadable, or malformed — clients should
//! always get *something* usable rather than error on startup.
//!
//! The daemon advertises `bind_host` as a listen address. `0.0.0.0` and
//! `::` are valid listen addresses but invalid dial addresses — a user who
//! sets `bind_host: 0.0.0.0` to expose the daemon on their LAN must still
//! connect to `127.0.0.1` locally. Normalization lives in [`daemon_url`]
//! (the caller concerned with dialing), not here; this module returns the
//! raw endpoint as written.
//!
//! [`daemon_url`]: crate::daemon_url

use std::path::{Path, PathBuf};

/// Default daemon port when bootstrap.yaml is missing or malformed.
pub const DEFAULT_DAEMON_PORT: u16 = 60887;

/// Default bind host when bootstrap.yaml is missing or malformed.
pub const DEFAULT_BIND_HOST: &str = "127.0.0.1";

const BOOTSTRAP_RELATIVE_PATH: &str = ".gobby/bootstrap.yaml";

/// A daemon endpoint as advertised by bootstrap.yaml.
///
/// `host` is returned verbatim from the config (or [`DEFAULT_BIND_HOST`]);
/// callers that dial should apply [`crate::daemon_url`] to normalize
/// unroutable listen addresses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonEndpoint {
    pub host: String,
    pub port: u16,
}

impl Default for DaemonEndpoint {
    fn default() -> Self {
        Self {
            host: DEFAULT_BIND_HOST.to_string(),
            port: DEFAULT_DAEMON_PORT,
        }
    }
}

/// Resolve the path to `~/.gobby/bootstrap.yaml`.
///
/// Returns `None` when the home directory cannot be determined.
pub fn bootstrap_path() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(BOOTSTRAP_RELATIVE_PATH))
}

/// Read the daemon endpoint from the default bootstrap path.
///
/// Falls back to [`DaemonEndpoint::default`] on any failure — missing file,
/// unreadable file, malformed YAML, missing fields, or no home directory.
pub fn read_daemon_endpoint() -> DaemonEndpoint {
    match bootstrap_path() {
        Some(path) => read_daemon_endpoint_at(&path),
        None => DaemonEndpoint::default(),
    }
}

/// Read the daemon endpoint from a specific bootstrap file path.
///
/// Exposed for tests and for callers that know the path explicitly.
/// Same fallback semantics as [`read_daemon_endpoint`].
pub fn read_daemon_endpoint_at(path: &Path) -> DaemonEndpoint {
    let Ok(contents) = std::fs::read_to_string(path) else {
        return DaemonEndpoint::default();
    };
    let Ok(yaml) = serde_yaml::from_str::<serde_yaml::Value>(&contents) else {
        return DaemonEndpoint::default();
    };

    let port = yaml
        .get("daemon_port")
        .and_then(|v| v.as_u64())
        .and_then(|n| u16::try_from(n).ok())
        .unwrap_or(DEFAULT_DAEMON_PORT);

    let host = yaml
        .get("bind_host")
        .and_then(|v| v.as_str())
        .map(str::to_owned)
        .unwrap_or_else(|| DEFAULT_BIND_HOST.to_string());

    DaemonEndpoint { host, port }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn missing_file_returns_defaults() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("does-not-exist.yaml");
        assert_eq!(read_daemon_endpoint_at(&path), DaemonEndpoint::default());
    }

    #[test]
    fn malformed_yaml_returns_defaults() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, ": : not valid yaml ::\n\t").unwrap();
        assert_eq!(read_daemon_endpoint_at(&path), DaemonEndpoint::default());
    }

    #[test]
    fn empty_file_returns_defaults() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "").unwrap();
        assert_eq!(read_daemon_endpoint_at(&path), DaemonEndpoint::default());
    }

    #[test]
    fn missing_fields_return_defaults() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "other_field: value\n").unwrap();
        assert_eq!(read_daemon_endpoint_at(&path), DaemonEndpoint::default());
    }

    #[test]
    fn reads_custom_port() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "daemon_port: 61234\n").unwrap();
        let ep = read_daemon_endpoint_at(&path);
        assert_eq!(ep.port, 61234);
        assert_eq!(ep.host, DEFAULT_BIND_HOST);
    }

    #[test]
    fn reads_custom_host_and_port() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "daemon_port: 60887\nbind_host: 0.0.0.0\n").unwrap();
        let ep = read_daemon_endpoint_at(&path);
        assert_eq!(ep.port, 60887);
        assert_eq!(ep.host, "0.0.0.0");
    }

    #[test]
    fn out_of_range_port_falls_back() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "daemon_port: 70000\n").unwrap();
        assert_eq!(read_daemon_endpoint_at(&path).port, DEFAULT_DAEMON_PORT);
    }
}
