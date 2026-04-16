//! Daemon URL composition with unroutable-host normalization.
//!
//! The Gobby daemon advertises a listen address in `bootstrap.yaml`, but
//! clients need a *dial* address. `0.0.0.0` and `::` are wildcard listen
//! addresses — you cannot `connect(2)` to them — so a user who sets
//! `bind_host: 0.0.0.0` (to expose the daemon on their LAN) must still
//! dial `127.0.0.1` from a local client. This module applies that
//! normalization uniformly; [`crate::bootstrap`] returns the raw endpoint.

use std::path::Path;

use crate::bootstrap::{DaemonEndpoint, read_daemon_endpoint, read_daemon_endpoint_at};

/// Compose the daemon dial URL from the default bootstrap path.
pub fn daemon_url() -> String {
    endpoint_to_url(&read_daemon_endpoint())
}

/// Compose the daemon dial URL from a specific bootstrap file path.
///
/// Exposed for tests and for callers that know the path explicitly.
pub fn daemon_url_at(path: &Path) -> String {
    endpoint_to_url(&read_daemon_endpoint_at(path))
}

fn endpoint_to_url(endpoint: &DaemonEndpoint) -> String {
    let host = dial_host(&endpoint.host);
    format!("http://{host}:{}", endpoint.port)
}

/// Map a listen host to a dial host.
///
/// Wildcard listen addresses (`0.0.0.0`, `::`, `::0`) are rewritten to
/// `127.0.0.1`. Everything else passes through unchanged — hostnames,
/// named interfaces, explicit IPv4/IPv6 literals. In practice
/// bootstrap.yaml is always `localhost`, an IPv4 literal, or a wildcard,
/// so bracketing IPv6 for URL embedding is not handled here.
fn dial_host(host: &str) -> &str {
    match host {
        "0.0.0.0" | "::" | "::0" => "127.0.0.1",
        other => other,
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
}
