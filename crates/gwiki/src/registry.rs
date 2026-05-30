use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

use crate::WikiError;
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
    let mut registry = read_registry(path)?;

    match scope.kind() {
        ScopeKind::Topic { name } => {
            registry
                .topics
                .entry(name.clone())
                .or_insert_with(|| TopicRegistration {
                    name: name.clone(),
                    path: scope.root().display().to_string(),
                });
        }
        ScopeKind::Project {
            project_id,
            project_root,
        } => {
            registry
                .projects
                .entry(project_id.clone())
                .or_insert_with(|| ProjectRegistration {
                    project_id: project_id.clone(),
                    project_root: project_root.display().to_string(),
                    path: scope.root().display().to_string(),
                });
        }
    }

    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create registry directory",
            path: Some(parent.to_path_buf()),
            source: error.to_string(),
        })?;
    }
    let contents =
        serde_json::to_string_pretty(&registry).map_err(|error| WikiError::Registry {
            detail: format!("failed to serialize {}: {error}", path.display()),
        })?;
    write_registry_atomically(path, format!("{contents}\n").as_bytes())
}

fn write_registry_atomically(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    let temp_path = temp_registry_path(path);
    let mut file = std::fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create registry temp file",
        path: Some(temp_path.clone()),
        source: error.to_string(),
    })?;
    file.write_all(contents).map_err(|error| WikiError::Io {
        action: "write registry temp file",
        path: Some(temp_path.clone()),
        source: error.to_string(),
    })?;
    file.sync_all().map_err(|error| WikiError::Io {
        action: "sync registry temp file",
        path: Some(temp_path.clone()),
        source: error.to_string(),
    })?;
    drop(file);
    std::fs::rename(&temp_path, path).map_err(|error| WikiError::Io {
        action: "replace registry",
        path: Some(path.to_path_buf()),
        source: error.to_string(),
    })?;
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

fn read_registry(path: &Path) -> Result<Registry, WikiError> {
    match std::fs::read_to_string(path) {
        Ok(contents) => serde_json::from_str(&contents).map_err(|error| WikiError::Registry {
            detail: format!("failed to parse {}: {error}", path.display()),
        }),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Registry::default()),
        Err(error) => Err(WikiError::Io {
            action: "read registry",
            path: Some(path.to_path_buf()),
            source: error.to_string(),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn register_preserves_existing_entries() {
        let tmp = tempfile::tempdir().expect("tempdir");
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
            tmp.path()
                .join("replacement")
                .join("topics")
                .join("existing"),
            tmp.path().join("replacement").join("wikis.json"),
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
            Some("/keep/topic")
        );
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
