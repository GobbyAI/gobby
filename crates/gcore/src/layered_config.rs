//! Layered YAML config loading shared by tool binaries (gsqz, gloc).
//!
//! Resolution order: CLI override → `.gobby/<tool>.yaml` in the current
//! directory → `.gobby/<tool>.yaml` at the project root (via
//! [`crate::project::find_project_root`], so running from a subdirectory still
//! finds the project layer) → `<gobby_home>/<tool>.yaml` (respects
//! `GOBBY_HOME` via [`crate::gobby_home`]) → `None`, where the caller falls
//! back to its compiled-in default. The first layer found wins entirely (no
//! merging), and a file that exists but does not parse is an error — a broken
//! config must never be silently skipped in favor of a lower layer.

use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;

#[derive(Debug, thiserror::Error)]
pub enum LayeredConfigError {
    #[error("could not read {}", path.display())]
    Read { path: PathBuf },
    #[error("failed to parse {}: {source}", path.display())]
    Parse {
        path: PathBuf,
        source: serde_yaml::Error,
    },
}

/// Load `<tool>.yaml` from the highest-priority layer that has it.
///
/// Returns `Ok(None)` when no layer provides a config file; the caller is
/// expected to fall back to its built-in default. The CLI override must be
/// readable, while the discovered layers skip missing files silently.
pub fn load_layered_yaml<T: DeserializeOwned>(
    tool: &str,
    cli_override: Option<&Path>,
) -> Result<Option<T>, LayeredConfigError> {
    if let Some(path) = cli_override {
        let content = std::fs::read_to_string(path).map_err(|_| LayeredConfigError::Read {
            path: path.to_path_buf(),
        })?;
        return parse(path, &content).map(Some);
    }

    let file_name = format!("{tool}.yaml");

    if let Some(config) = try_layer(&Path::new(".gobby").join(&file_name))? {
        return Ok(Some(config));
    }

    if let Ok(cwd) = std::env::current_dir()
        && let Some(root) = crate::project::find_project_root(&cwd)
        && let Some(config) = try_layer(&root.join(".gobby").join(&file_name))?
    {
        return Ok(Some(config));
    }

    if let Ok(home) = crate::gobby_home()
        && let Some(config) = try_layer(&home.join(&file_name))?
    {
        return Ok(Some(config));
    }

    Ok(None)
}

fn try_layer<T: DeserializeOwned>(path: &Path) -> Result<Option<T>, LayeredConfigError> {
    let Ok(content) = std::fs::read_to_string(path) else {
        return Ok(None);
    };
    parse(path, &content).map(Some)
}

fn parse<T: DeserializeOwned>(path: &Path, content: &str) -> Result<T, LayeredConfigError> {
    serde_yaml::from_str(content).map_err(|source| LayeredConfigError::Parse {
        path: path.to_path_buf(),
        source,
    })
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use tempfile::{TempDir, tempdir};

    use super::*;

    #[derive(Debug, PartialEq, serde::Deserialize)]
    struct TestConfig {
        name: String,
    }

    /// Serializes CWD and `GOBBY_HOME` mutation through `TEST_ENV_LOCK` and
    /// restores both on drop.
    struct CwdGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
        previous_cwd: PathBuf,
        previous_home: Option<std::ffi::OsString>,
    }

    impl CwdGuard {
        fn enter(cwd: &Path, gobby_home: &Path) -> Self {
            let lock = crate::config::TEST_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let previous_cwd = std::env::current_dir().expect("current dir");
            let previous_home = std::env::var_os("GOBBY_HOME");
            std::env::set_current_dir(cwd).expect("enter test cwd");
            // SAFETY: GOBBY_HOME mutation is serialized through TEST_ENV_LOCK
            // and restored in Drop before the lock is released.
            unsafe { std::env::set_var("GOBBY_HOME", gobby_home) };
            Self {
                _lock: lock,
                previous_cwd,
                previous_home,
            }
        }
    }

    impl Drop for CwdGuard {
        fn drop(&mut self) {
            let _ = std::env::set_current_dir(&self.previous_cwd);
            // SAFETY: still holding TEST_ENV_LOCK; restores the original value.
            unsafe {
                match &self.previous_home {
                    Some(value) => std::env::set_var("GOBBY_HOME", value),
                    None => std::env::remove_var("GOBBY_HOME"),
                }
            }
        }
    }

    fn project_with_config(config_yaml: &str) -> TempDir {
        let project = tempdir().expect("project dir");
        let gobby_dir = project.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).expect("create .gobby");
        std::fs::write(gobby_dir.join("project.json"), r#"{"id": "p1"}"#)
            .expect("write project.json");
        std::fs::write(gobby_dir.join("test-tool.yaml"), config_yaml).expect("write config");
        project
    }

    #[test]
    fn subdirectory_resolves_project_root_config() {
        let project = project_with_config("name: from-project-root\n");
        let nested = project.path().join("nested/deeper");
        std::fs::create_dir_all(&nested).expect("create subdirectory");
        let home = tempdir().expect("home dir");
        let _guard = CwdGuard::enter(&nested, home.path());

        let config: Option<TestConfig> =
            load_layered_yaml("test-tool", None).expect("layered load");

        assert_eq!(
            config,
            Some(TestConfig {
                name: "from-project-root".to_string()
            })
        );
    }

    #[test]
    fn cwd_config_wins_without_project_marker() {
        let dir = tempdir().expect("cwd dir");
        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).expect("create .gobby");
        std::fs::write(gobby_dir.join("test-tool.yaml"), "name: from-cwd\n").expect("write config");
        let home = tempdir().expect("home dir");
        let _guard = CwdGuard::enter(dir.path(), home.path());

        let config: Option<TestConfig> =
            load_layered_yaml("test-tool", None).expect("layered load");

        assert_eq!(
            config,
            Some(TestConfig {
                name: "from-cwd".to_string()
            })
        );
    }

    #[test]
    fn cli_override_beats_discovered_layers() {
        let project = project_with_config("name: from-project-root\n");
        let override_path = project.path().join("override.yaml");
        std::fs::write(&override_path, "name: from-override\n").expect("write override");
        let home = tempdir().expect("home dir");
        let _guard = CwdGuard::enter(project.path(), home.path());

        let config: Option<TestConfig> =
            load_layered_yaml("test-tool", Some(&override_path)).expect("layered load");

        assert_eq!(
            config,
            Some(TestConfig {
                name: "from-override".to_string()
            })
        );
    }

    #[test]
    fn unreadable_cli_override_is_an_error() {
        let dir = tempdir().expect("cwd dir");
        let home = tempdir().expect("home dir");
        let _guard = CwdGuard::enter(dir.path(), home.path());

        let error = load_layered_yaml::<TestConfig>("test-tool", Some(Path::new("missing.yaml")))
            .expect_err("missing override must error");

        assert!(matches!(error, LayeredConfigError::Read { .. }));
    }

    #[test]
    fn gobby_home_layer_is_used_when_no_project_config() {
        let dir = tempdir().expect("cwd dir");
        let home = tempdir().expect("home dir");
        std::fs::write(
            home.path().join("test-tool.yaml"),
            "name: from-gobby-home\n",
        )
        .expect("write global config");
        let _guard = CwdGuard::enter(dir.path(), home.path());

        let config: Option<TestConfig> =
            load_layered_yaml("test-tool", None).expect("layered load");

        assert_eq!(
            config,
            Some(TestConfig {
                name: "from-gobby-home".to_string()
            })
        );
    }

    #[test]
    fn broken_layer_config_is_an_error_not_a_fallthrough() {
        let project = project_with_config(": not yaml [");
        let home = tempdir().expect("home dir");
        std::fs::write(
            home.path().join("test-tool.yaml"),
            "name: from-gobby-home\n",
        )
        .expect("write global config");
        let _guard = CwdGuard::enter(project.path(), home.path());

        let error = load_layered_yaml::<TestConfig>("test-tool", None)
            .expect_err("broken project config must not fall through");

        assert!(matches!(error, LayeredConfigError::Parse { .. }));
    }

    #[test]
    fn no_layer_found_returns_none() {
        let dir = tempdir().expect("cwd dir");
        let home = tempdir().expect("home dir");
        let _guard = CwdGuard::enter(dir.path(), home.path());

        let config: Option<TestConfig> =
            load_layered_yaml("test-tool", None).expect("layered load");

        assert_eq!(config, None);
    }
}
