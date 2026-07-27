use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use thiserror::Error;

pub const RUNTIME_MODE_ENV: &str = "GOBBY_RUNTIME_MODE";
const DAEMON_URL_ENV: &str = "GOBBY_DAEMON_URL";

const MACOS_SERVICE_PATH: &str = "Library/LaunchAgents/com.gobby.daemon.plist";
const LINUX_SERVICE_PATH: &str = "systemd/user/gobby-daemon.service";
const WINDOWS_SERVICE_FILENAME: &str = "gobby-daemon.task.xml";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeMode {
    Daemon,
    Standalone,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StandaloneOverride {
    Standalone,
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum RuntimeModeError {
    #[error(
        "invalid {RUNTIME_MODE_ENV} value {value:?}; expected \"auto\", \"standalone\", or an empty value"
    )]
    InvalidValue { value: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RuntimePlatform {
    MacOs,
    Linux,
    Windows,
    Unsupported,
}

static RUNTIME_MODE: OnceLock<RuntimeMode> = OnceLock::new();

/// Return the process-lifetime Rust runtime mode.
///
/// Environment and service-registration changes made after the first
/// successful call take effect on the next process invocation.
pub fn runtime_mode() -> Result<RuntimeMode, RuntimeModeError> {
    if let Some(mode) = RUNTIME_MODE.get() {
        return Ok(*mode);
    }

    let selected = select_runtime_mode_with_probe(
        std::env::var_os(RUNTIME_MODE_ENV).as_deref(),
        std::env::var_os(DAEMON_URL_ENV).as_deref(),
        service_is_registered,
    )?;
    Ok(*RUNTIME_MODE.get_or_init(|| selected))
}

fn select_runtime_mode_with_probe(
    requested_mode: Option<&OsStr>,
    daemon_url: Option<&OsStr>,
    service_registered: impl FnOnce() -> bool,
) -> Result<RuntimeMode, RuntimeModeError> {
    match parse_requested_mode(requested_mode)? {
        Some(StandaloneOverride::Standalone) => return Ok(RuntimeMode::Standalone),
        None => {}
    }

    if daemon_url.is_some_and(|value| !value.is_empty()) {
        return Ok(RuntimeMode::Daemon);
    }
    if service_registered() {
        return Ok(RuntimeMode::Daemon);
    }
    Ok(RuntimeMode::Standalone)
}

fn parse_requested_mode(
    value: Option<&OsStr>,
) -> Result<Option<StandaloneOverride>, RuntimeModeError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_empty() {
        return Ok(None);
    }
    match value.to_str() {
        Some("auto") => Ok(None),
        Some("standalone") => Ok(Some(StandaloneOverride::Standalone)),
        Some(value) => Err(RuntimeModeError::InvalidValue {
            value: value.to_string(),
        }),
        None => Err(RuntimeModeError::InvalidValue {
            value: "<non-UTF-8>".to_string(),
        }),
    }
}

fn service_is_registered() -> bool {
    service_registration_path().is_some_and(|path| path.exists())
}

fn service_registration_path() -> Option<PathBuf> {
    let home_dir = dirs::home_dir();
    let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME");
    let gobby_home = crate::gobby_home().ok();
    service_registration_path_for(
        current_platform(),
        home_dir.as_deref(),
        xdg_config_home.as_deref(),
        gobby_home.as_deref(),
    )
}

fn current_platform() -> RuntimePlatform {
    if cfg!(target_os = "macos") {
        RuntimePlatform::MacOs
    } else if cfg!(target_os = "linux") {
        RuntimePlatform::Linux
    } else if cfg!(target_os = "windows") {
        RuntimePlatform::Windows
    } else {
        RuntimePlatform::Unsupported
    }
}

fn service_registration_path_for(
    platform: RuntimePlatform,
    home_dir: Option<&Path>,
    xdg_config_home: Option<&OsStr>,
    gobby_home: Option<&Path>,
) -> Option<PathBuf> {
    match platform {
        RuntimePlatform::MacOs => Some(home_dir?.join(MACOS_SERVICE_PATH)),
        RuntimePlatform::Linux => {
            let config_home = xdg_config_home
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
                .or_else(|| home_dir.map(|home| home.join(".config")))?;
            Some(config_home.join(LINUX_SERVICE_PATH))
        }
        RuntimePlatform::Windows => Some(gobby_home?.join(WINDOWS_SERVICE_FILENAME)),
        RuntimePlatform::Unsupported => None,
    }
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::ffi::OsStr;
    use std::path::Path;

    use super::*;

    #[test]
    fn mode_precedence_is_standalone_then_url_then_registration() {
        let probes = Cell::new(0);
        let standalone = select_runtime_mode_with_probe(
            Some(OsStr::new("standalone")),
            Some(OsStr::new("https://daemon.example")),
            || {
                probes.set(probes.get() + 1);
                true
            },
        )
        .expect("standalone mode");
        assert_eq!(standalone, RuntimeMode::Standalone);
        assert_eq!(probes.get(), 0);

        let remote = select_runtime_mode_with_probe(
            Some(OsStr::new("auto")),
            Some(OsStr::new("https://daemon.example")),
            || {
                probes.set(probes.get() + 1);
                false
            },
        )
        .expect("remote daemon mode");
        assert_eq!(remote, RuntimeMode::Daemon);
        assert_eq!(probes.get(), 0);

        let installed = select_runtime_mode_with_probe(None, None, || {
            probes.set(probes.get() + 1);
            true
        })
        .expect("installed daemon mode");
        assert_eq!(installed, RuntimeMode::Daemon);
        assert_eq!(probes.get(), 1);

        let absent = select_runtime_mode_with_probe(Some(OsStr::new("")), None, || {
            probes.set(probes.get() + 1);
            false
        })
        .expect("standalone mode without installation");
        assert_eq!(absent, RuntimeMode::Standalone);
        assert_eq!(probes.get(), 2);

        let explicit_auto = select_runtime_mode_with_probe(Some(OsStr::new("auto")), None, || {
            probes.set(probes.get() + 1);
            false
        })
        .expect("auto mode without installation");
        assert_eq!(explicit_auto, RuntimeMode::Standalone);
        assert_eq!(probes.get(), 3);
    }

    #[test]
    fn invalid_runtime_mode_values_are_configuration_errors() {
        for value in ["daemon", "AUTO", " standalone "] {
            assert_eq!(
                parse_requested_mode(Some(OsStr::new(value))),
                Err(RuntimeModeError::InvalidValue {
                    value: value.to_string(),
                })
            );
        }
    }

    #[test]
    fn service_registration_paths_match_each_installer_artifact() {
        let home = Path::new("/home/alice");
        let gobby_home = Path::new("/gobby/home");

        assert_eq!(
            service_registration_path_for(RuntimePlatform::MacOs, Some(home), None, None),
            Some(home.join("Library/LaunchAgents/com.gobby.daemon.plist"))
        );
        assert_eq!(
            service_registration_path_for(RuntimePlatform::Linux, Some(home), None, None),
            Some(home.join(".config/systemd/user/gobby-daemon.service"))
        );
        assert_eq!(
            service_registration_path_for(
                RuntimePlatform::Linux,
                Some(home),
                Some(OsStr::new("/xdg/config")),
                None,
            ),
            Some(PathBuf::from(
                "/xdg/config/systemd/user/gobby-daemon.service"
            ))
        );
        assert_eq!(
            service_registration_path_for(
                RuntimePlatform::Windows,
                Some(home),
                None,
                Some(gobby_home),
            ),
            Some(gobby_home.join("gobby-daemon.task.xml"))
        );
        assert_eq!(
            service_registration_path_for(
                RuntimePlatform::Unsupported,
                Some(home),
                Some(OsStr::new("/xdg/config")),
                Some(gobby_home),
            ),
            None
        );
    }
}
