use std::collections::BTreeMap;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::WikiError;
use crate::compile::index_lock_timeout;
use crate::scope::{ResolvedScope, ScopeKind};

#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Registry {
    #[serde(default)]
    topics: BTreeMap<String, TopicRegistration>,
    #[serde(default)]
    projects: BTreeMap<String, ProjectRegistration>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TopicRegistration {
    name: String,
    path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectRegistration {
    project_id: String,
    project_root: String,
    path: String,
}

pub fn register_scope(path: &Path, scope: &ResolvedScope) -> Result<(), WikiError> {
    let _claim = crate::singleton::ensure_maintenance()?;
    ensure_registry_parent(path)?;
    let lock_path = registry_lock_path(path);
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open registry lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    lock_registry(&lock, &lock_path)?;

    let mut registry = read_registry(path)?;

    match scope.kind() {
        ScopeKind::Topic { name } => {
            registry.topics.insert(
                name.clone(),
                TopicRegistration {
                    name: name.clone(),
                    path: registration_path(scope, name)?,
                },
            );
        }
        ScopeKind::Project {
            project_id,
            project_root,
        } => {
            let stored_path = if project_id == gobby_core::project::PERSONAL_PROJECT_ID {
                registration_path(scope, "personal")?
            } else {
                scope.root().display().to_string()
            };
            registry.projects.insert(
                project_id.clone(),
                ProjectRegistration {
                    project_id: project_id.clone(),
                    project_root: project_root.display().to_string(),
                    path: stored_path,
                },
            );
        }
    }

    let contents =
        serde_json::to_string_pretty(&registry).map_err(|error| WikiError::Registry {
            detail: format!("failed to serialize {}: {error}", path.display()),
        })?;
    let write_result = write_registry_atomically(path, format!("{contents}\n").as_bytes());
    let unlock_result = fs4::FileExt::unlock(&lock).map_err(|error| WikiError::Io {
        action: "unlock registry",
        path: Some(lock_path),
        source: error,
    });

    match write_result {
        Ok(()) => unlock_result,
        Err(error) => {
            let _ = unlock_result;
            Err(error)
        }
    }
}

fn ensure_registry_parent(path: &Path) -> Result<(), WikiError> {
    let Some(parent) = path.parent() else {
        return Ok(());
    };
    if let Some(owner) = crate::owner_fs::owner_for_files_home()?
        && let Ok(relative) = parent.strip_prefix(owner.path())
    {
        return owner.create_dir_all(relative);
    }
    std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
        action: "create registry directory",
        path: Some(parent.to_path_buf()),
        source: error,
    })
}

fn registration_path(scope: &ResolvedScope, expected_name: &str) -> Result<String, WikiError> {
    let parent = scope
        .registry_path()
        .parent()
        .ok_or_else(|| WikiError::Registry {
            detail: format!("registry {} has no parent", scope.registry_path().display()),
        })?;
    let relative = scope
        .root()
        .strip_prefix(parent)
        .map_err(|_| WikiError::Registry {
            detail: format!(
                "scope root {} escapes registry parent {}",
                scope.root().display(),
                parent.display()
            ),
        })?;
    if relative.components().count() != 1 {
        return Err(WikiError::Registry {
            detail: format!(
                "scope root {} is not a direct child of {}",
                scope.root().display(),
                parent.display()
            ),
        });
    }
    if relative.as_os_str() != expected_name || expected_name == "topics" {
        return Err(WikiError::Registry {
            detail: format!(
                "refusing escaped or reserved registry path {}",
                relative.display()
            ),
        });
    }
    Ok(expected_name.to_string())
}

fn lock_registry(lock: &std::fs::File, lock_path: &Path) -> Result<(), WikiError> {
    let timeout = index_lock_timeout();
    let started = Instant::now();
    let mut retry_delay = registry_lock_initial_delay();

    loop {
        match fs4::FileExt::try_lock(lock) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= timeout {
                    return Err(WikiError::Io {
                        action: "lock registry",
                        path: Some(lock_path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", timeout.as_millis()),
                        ),
                    });
                }
                thread::sleep(retry_delay.min(timeout - elapsed));
                retry_delay = next_registry_lock_delay(retry_delay);
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "lock registry",
                    path: Some(lock_path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
}

fn registry_lock_initial_delay() -> Duration {
    Duration::from_millis(25)
}

fn next_registry_lock_delay(current: Duration) -> Duration {
    current.saturating_mul(2).min(Duration::from_millis(250))
}

fn write_registry_atomically(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    let temp_path = temp_registry_path(path);
    let mut file = std::fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create registry temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write registry temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = std::fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync registry temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = std::fs::rename(&temp_path, path) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "replace registry",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    if let Some(parent) = path.parent()
        && let Ok(directory) = std::fs::File::open(parent)
    {
        let _ = directory.sync_all();
    }
    Ok(())
}

fn temp_registry_path(path: &Path) -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("wikis.json");
    let counter = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(
        ".{file_name}.{}.{}.{}.tmp",
        std::process::id(),
        counter,
        nanos
    ))
}

fn registry_lock_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("wikis.json");
    path.with_file_name(format!("{file_name}.lock"))
}

fn read_registry(path: &Path) -> Result<Registry, WikiError> {
    match std::fs::read_to_string(path) {
        Ok(contents) => serde_json::from_str(&contents).map_err(|error| WikiError::Registry {
            detail: format!("failed to parse {}: {error}", path.display()),
        }),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Registry::default()),
        Err(error) => Err(WikiError::Io {
            action: "read registry",
            path: Some(path.to_path_buf()),
            source: error,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn registry_lock_retry_delay_backs_off_exponentially() {
        let mut delay = registry_lock_initial_delay();

        assert_eq!(delay, Duration::from_millis(25));
        delay = next_registry_lock_delay(delay);
        assert_eq!(delay, Duration::from_millis(50));
        delay = next_registry_lock_delay(delay);
        assert_eq!(delay, Duration::from_millis(100));
        delay = next_registry_lock_delay(delay);
        assert_eq!(delay, Duration::from_millis(200));
        delay = next_registry_lock_delay(delay);
        assert_eq!(delay, Duration::from_millis(250));
        delay = next_registry_lock_delay(delay);
        assert_eq!(delay, Duration::from_millis(250));
    }

    #[test]
    #[serial_test::serial]
    fn register_overwrites_existing_entries() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path().join("gobby-home");
        fs::create_dir_all(&home).expect("gobby home");
        let _env = crate::support::test_env::EnvGuard::set("GOBBY_HOME", home.as_os_str());
        let registry = tmp.path().join("wikis.json");
        fs::write(
            &registry,
            r#"{
  "topics": {
    "existing": {
      "name": "existing",
      "path": "/keep/topic"
    }
  },
  "projects": {
    "project-1": {
      "project_id": "project-1",
      "project_root": "/keep/project-root",
      "path": "/keep/project"
    }
  }
}
"#,
        )
        .expect("seed registry");

        let existing = crate::scope::ResolvedScope::topic(
            "existing".to_string(),
            tmp.path().join("existing"),
            registry.clone(),
        );
        register_scope(&registry, &existing).expect("register existing topic");

        let new_project = crate::scope::ResolvedScope::project(
            "project-2".to_string(),
            tmp.path().join("project-2"),
            tmp.path().join("project-2").join(".gobby").join("wiki"),
        );
        register_scope(&registry, &new_project).expect("register new project");

        let stored = fs::read_to_string(&registry).expect("read registry");
        let stored: Registry = serde_json::from_str(&stored).expect("parse registry");

        assert_eq!(
            stored
                .topics
                .get("existing")
                .map(|topic| topic.path.as_str()),
            Some("existing")
        );
        assert!(
            !stored
                .topics
                .get("existing")
                .expect("topic")
                .path
                .contains("topics/")
        );

        let escaped = crate::scope::ResolvedScope::topic(
            "escaped".to_string(),
            tmp.path().join("outside").join("escaped"),
            registry.clone(),
        );
        assert!(register_scope(&registry, &escaped).is_err());
        assert_eq!(
            stored
                .projects
                .get("project-1")
                .map(|project| project.path.as_str()),
            Some("/keep/project")
        );
        assert_eq!(
            stored
                .projects
                .get("project-2")
                .map(|project| project.project_root.as_str()),
            Some(tmp.path().join("project-2").display().to_string().as_str())
        );
    }

    #[test]
    fn temp_registry_paths_are_unique_in_registry_directory() {
        let path = Path::new("/tmp/wiki/wikis.json");
        let first = temp_registry_path(path);
        let second = temp_registry_path(path);

        assert_ne!(first, second);
        assert_eq!(first.parent(), path.parent());
        assert_eq!(second.parent(), path.parent());
    }
}
