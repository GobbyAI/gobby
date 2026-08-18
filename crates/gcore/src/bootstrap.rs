//! Bootstrap config resolution.
//!
//! Reads `~/.gobby/bootstrap.yaml` to discover how the Gobby daemon is
//! reachable: either an explicit dial URL or its TCP port and bind host.
//! Falls back to loopback defaults when the file is missing, unreadable, or
//! malformed — clients should always get *something* usable rather than error
//! on startup.
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

const BOOTSTRAP_FILENAME: &str = "bootstrap.yaml";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubDatabaseBootstrap {
    pub database_url: Option<String>,
    pub daemon_url: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DatastoreMode {
    Local,
    Remote,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilesHomeView {
    pub datastore_mode: DatastoreMode,
    pub files_home: Option<PathBuf>,
    pub hub_daemon_url: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum FilesHomeError {
    #[error("{0}")]
    Invalid(String),
}

/// A daemon endpoint as advertised by bootstrap.yaml.
///
/// `host` is returned verbatim from the config (or [`DEFAULT_BIND_HOST`]);
/// callers that dial should apply [`crate::daemon_url`] to normalize
/// unroutable listen addresses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonEndpoint {
    pub daemon_url: Option<String>,
    pub host: String,
    pub port: u16,
}

impl Default for DaemonEndpoint {
    fn default() -> Self {
        Self {
            daemon_url: None,
            host: DEFAULT_BIND_HOST.to_string(),
            port: DEFAULT_DAEMON_PORT,
        }
    }
}

/// Resolve the path to `bootstrap.yaml` inside the Gobby home directory.
///
/// Respects `GOBBY_HOME` via [`crate::gobby_home`], falling back to
/// `~/.gobby`. Returns `None` when neither `GOBBY_HOME` nor the home
/// directory can be determined.
pub fn bootstrap_path() -> Option<PathBuf> {
    crate::gobby_home().ok().map(|h| h.join(BOOTSTRAP_FILENAME))
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

    let daemon_url = yaml
        .get("daemon_url")
        .and_then(|v| v.as_str())
        .and_then(non_empty_trimmed);

    DaemonEndpoint {
        daemon_url,
        host,
        port,
    }
}

pub fn read_hub_database_bootstrap_file(
    path: &Path,
) -> anyhow::Result<Option<HubDatabaseBootstrap>> {
    if !path.exists() {
        return Ok(None);
    }

    let contents = std::fs::read_to_string(path).map_err(|error| {
        anyhow::anyhow!(
            "failed to read Gobby bootstrap at {}: {error}",
            path.display()
        )
    })?;
    parse_hub_database_bootstrap(&contents)
        .map_err(|error| anyhow::anyhow!("failed to parse {}: {error}", path.display()))
}

pub fn parse_hub_database_bootstrap(
    contents: &str,
) -> anyhow::Result<Option<HubDatabaseBootstrap>> {
    if contents.trim().is_empty() {
        return Ok(None);
    }
    let yaml: serde_yaml::Value = serde_yaml::from_str(contents)?;
    if yaml.is_null() {
        return Ok(None);
    }
    let Some(map) = yaml.as_mapping() else {
        anyhow::bail!("bootstrap.yaml must be a mapping");
    };

    Ok(Some(HubDatabaseBootstrap {
        database_url: optional_string_field(map, "database_url")?,
        daemon_url: optional_string_field(map, "daemon_url")?,
    }))
}

pub fn postgres_database_url_from_bootstrap_file(path: &Path) -> anyhow::Result<Option<String>> {
    let Some(bootstrap) = read_hub_database_bootstrap_file(path)? else {
        return Ok(None);
    };
    Ok(postgres_database_url_from_bootstrap(&bootstrap))
}

pub fn postgres_database_url_from_bootstrap(bootstrap: &HubDatabaseBootstrap) -> Option<String> {
    bootstrap.database_url.clone()
}

/// Read the typed files-home view from a bootstrap file.
///
/// A missing file is owner-less local defaults. A present file is fail-closed.
pub fn read_files_home_view_at(path: &Path) -> Result<FilesHomeView, FilesHomeError> {
    if !path.exists() {
        return Ok(FilesHomeView {
            datastore_mode: DatastoreMode::Local,
            files_home: None,
            hub_daemon_url: None,
        });
    }
    let contents = std::fs::read_to_string(path).map_err(|error| {
        FilesHomeError::Invalid(format!("failed to read bootstrap.yaml: {error}"))
    })?;
    parse_files_home_view(&contents)
}

pub fn parse_files_home_view(contents: &str) -> Result<FilesHomeView, FilesHomeError> {
    let yaml: serde_yaml::Value = serde_yaml::from_str(contents).map_err(|error| {
        FilesHomeError::Invalid(format!("bootstrap.yaml is not valid YAML: {error}"))
    })?;
    let Some(map) = yaml.as_mapping() else {
        return Err(FilesHomeError::Invalid(
            "bootstrap.yaml must contain a YAML mapping".to_string(),
        ));
    };
    let datastore_mode = match optional_string_field(map, "datastore_mode")
        .map_err(|error| FilesHomeError::Invalid(error.to_string()))?
        .as_deref()
    {
        None | Some("local") => DatastoreMode::Local,
        Some("remote") => DatastoreMode::Remote,
        Some(other) => {
            return Err(FilesHomeError::Invalid(format!(
                "datastore_mode must be one of: local, remote (got {other})"
            )));
        }
    };
    let files_home = optional_string_field(map, "files_home")
        .map_err(|error| FilesHomeError::Invalid(error.to_string()))?;
    let hub_daemon_url = optional_string_field(map, "hub_daemon_url")
        .map_err(|error| FilesHomeError::Invalid(error.to_string()))?;
    match datastore_mode {
        DatastoreMode::Local => {
            if hub_daemon_url.is_some() {
                return Err(FilesHomeError::Invalid(
                    "hub_daemon_url is not allowed on a local bootstrap".to_string(),
                ));
            }
            let Some(raw) = files_home else {
                return Err(FilesHomeError::Invalid(
                    "files_home is required for datastore_mode: local".to_string(),
                ));
            };
            Ok(FilesHomeView {
                datastore_mode,
                files_home: Some(parse_absolute_files_home(&raw)?),
                hub_daemon_url: None,
            })
        }
        DatastoreMode::Remote => {
            if files_home.is_some() {
                return Err(FilesHomeError::Invalid(
                    "files_home is not allowed on a remote bootstrap".to_string(),
                ));
            }
            let Some(raw) = hub_daemon_url else {
                return Err(FilesHomeError::Invalid(
                    "hub_daemon_url is required for datastore_mode: remote".to_string(),
                ));
            };
            Ok(FilesHomeView {
                datastore_mode,
                files_home: None,
                hub_daemon_url: Some(parse_hub_origin(&raw)?),
            })
        }
    }
}

fn parse_absolute_files_home(value: &str) -> Result<PathBuf, FilesHomeError> {
    if value.starts_with('~') || !value.starts_with('/') {
        return Err(FilesHomeError::Invalid(
            "files_home must be an absolute path without ~".to_string(),
        ));
    }
    let path = PathBuf::from(value);
    if path.parent() == Some(Path::new("")) || path.parent() == Some(&path) {
        return Err(FilesHomeError::Invalid(
            "files_home must not be a filesystem root".to_string(),
        ));
    }
    Ok(path)
}

fn parse_hub_origin(value: &str) -> Result<String, FilesHomeError> {
    let trimmed = value.trim().trim_end_matches('/');
    let (scheme, rest) = if let Some(rest) = trimmed.strip_prefix("https://") {
        ("https", rest)
    } else if let Some(rest) = trimmed.strip_prefix("http://") {
        ("http", rest)
    } else {
        return Err(FilesHomeError::Invalid(
            "hub_daemon_url must be an http(s) origin with a host".to_string(),
        ));
    };
    if rest.is_empty()
        || rest.contains('@')
        || rest.contains('?')
        || rest.contains('#')
        || rest.contains('/')
    {
        return Err(FilesHomeError::Invalid(
            "hub_daemon_url must be an http(s) origin with a host".to_string(),
        ));
    }
    Ok(format!("{scheme}://{rest}"))
}

fn optional_string_field(map: &serde_yaml::Mapping, name: &str) -> anyhow::Result<Option<String>> {
    let key = serde_yaml::Value::String(name.to_string());
    match map.get(&key) {
        Some(value) => match value.as_str() {
            Some(text) => Ok(non_empty_trimmed(text)),
            None => anyhow::bail!("bootstrap.yaml field `{name}` must be a string"),
        },
        None => Ok(None),
    }
}

fn non_empty_trimmed(value: &str) -> Option<String> {
    let trimmed = value.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_string())
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
    fn reads_bootstrap_with_datastore_mode() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(
            &path,
            "datastore_mode: remote\ndaemon_port: 61234\nbind_host: 127.0.0.1\n",
        )
        .unwrap();

        let ep = read_daemon_endpoint_at(&path);

        assert_eq!(ep.port, 61234);
        assert_eq!(ep.host, "127.0.0.1");
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
    fn reads_explicit_daemon_url() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(
            &path,
            "daemon_url: '  http://gobby.example.test:62000/  '\n\
             daemon_port: 60887\n\
             bind_host: 0.0.0.0\n",
        )
        .unwrap();
        let ep = read_daemon_endpoint_at(&path);
        assert_eq!(
            ep.daemon_url.as_deref(),
            Some("http://gobby.example.test:62000/")
        );
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

    #[test]
    fn bootstrap_path_respects_gobby_home() {
        let _lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let previous = std::env::var_os("GOBBY_HOME");
        let dir = tempdir().unwrap();
        // SAFETY: GOBBY_HOME mutation is serialized through TEST_ENV_LOCK and
        // restored before the lock is released.
        unsafe { std::env::set_var("GOBBY_HOME", dir.path()) };
        let path = bootstrap_path();
        // SAFETY: still holding TEST_ENV_LOCK; restores the original value.
        unsafe {
            match &previous {
                Some(value) => std::env::set_var("GOBBY_HOME", value),
                None => std::env::remove_var("GOBBY_HOME"),
            }
        }
        assert_eq!(path, Some(dir.path().join("bootstrap.yaml")));
    }

    #[test]
    fn postgres_database_url_missing_file_returns_none() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("does-not-exist.yaml");

        assert_eq!(
            postgres_database_url_from_bootstrap_file(&path).unwrap(),
            None
        );
    }

    #[test]
    fn postgres_database_url_empty_file_returns_none() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "").unwrap();

        assert_eq!(
            postgres_database_url_from_bootstrap_file(&path).unwrap(),
            None
        );
    }

    #[test]
    fn postgres_database_url_null_file_returns_none() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "null\n").unwrap();

        assert_eq!(
            postgres_database_url_from_bootstrap_file(&path).unwrap(),
            None
        );
    }

    #[test]
    fn postgres_database_url_reads_postgres_url() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "database_url: postgresql://localhost/gobby\n").unwrap();

        assert_eq!(
            postgres_database_url_from_bootstrap_file(&path)
                .unwrap()
                .as_deref(),
            Some("postgresql://localhost/gobby")
        );
    }

    #[test]
    fn postgres_database_url_trims_url() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "database_url: '  postgresql://localhost/gobby  '\n").unwrap();

        assert_eq!(
            postgres_database_url_from_bootstrap_file(&path)
                .unwrap()
                .as_deref(),
            Some("postgresql://localhost/gobby")
        );
    }

    #[test]
    fn hub_database_bootstrap_reads_daemon_url() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(
            &path,
            "database_url: postgresql://localhost/gobby\n\
             daemon_url: '  http://gobby.example.test:62000/  '\n",
        )
        .unwrap();

        let bootstrap = read_hub_database_bootstrap_file(&path).unwrap().unwrap();

        assert_eq!(
            bootstrap.daemon_url.as_deref(),
            Some("http://gobby.example.test:62000/")
        );
    }

    #[test]
    fn postgres_database_url_ignores_legacy_backend_key() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(
            &path,
            "hub_backend: local-file\ndatabase_url: postgresql://localhost/gobby\n",
        )
        .unwrap();

        assert_eq!(
            postgres_database_url_from_bootstrap_file(&path)
                .unwrap()
                .as_deref(),
            Some("postgresql://localhost/gobby")
        );
    }

    #[test]
    fn postgres_database_url_malformed_yaml_errors() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "database_url: [").unwrap();

        assert!(postgres_database_url_from_bootstrap_file(&path).is_err());
    }

    #[test]
    fn postgres_database_url_non_empty_scalar_yaml_errors() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bootstrap.yaml");
        fs::write(&path, "postgres\n").unwrap();

        assert!(postgres_database_url_from_bootstrap_file(&path).is_err());
    }

    #[test]
    fn reads_bootstrap_with_files_home() {
        let dir = tempdir().unwrap();
        let owner = dir.path().join("owner.yaml");
        fs::write(
            &owner,
            "datastore_mode: local\nfiles_home: /var/lib/gobby/files\ndaemon_port: 61234\n",
        )
        .unwrap();
        let owner_view = read_files_home_view_at(&owner).unwrap();
        assert_eq!(owner_view.datastore_mode, DatastoreMode::Local);
        assert_eq!(
            owner_view.files_home.as_deref(),
            Some(Path::new("/var/lib/gobby/files"))
        );
        assert_eq!(owner_view.hub_daemon_url, None);
        let owner_endpoint = read_daemon_endpoint_at(&owner);
        assert_eq!(owner_endpoint.port, 61234);

        let remote = dir.path().join("remote.yaml");
        fs::write(
            &remote,
            "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887/\n",
        )
        .unwrap();
        let remote_view = read_files_home_view_at(&remote).unwrap();
        assert_eq!(remote_view.datastore_mode, DatastoreMode::Remote);
        assert_eq!(
            remote_view.hub_daemon_url.as_deref(),
            Some("http://hub.example.test:60887")
        );

        let missing = dir.path().join("missing.yaml");
        let missing_view = read_files_home_view_at(&missing).unwrap();
        assert_eq!(missing_view.files_home, None);
        assert_eq!(missing_view.hub_daemon_url, None);
        assert_eq!(read_daemon_endpoint_at(&missing), DaemonEndpoint::default());

        let relative = dir.path().join("relative.yaml");
        fs::write(&relative, "datastore_mode: local\nfiles_home: files\n").unwrap();
        assert!(read_files_home_view_at(&relative).is_err());

        let tilde = dir.path().join("tilde.yaml");
        fs::write(&tilde, "datastore_mode: local\nfiles_home: ~/files\n").unwrap();
        assert!(read_files_home_view_at(&tilde).is_err());
    }
}
