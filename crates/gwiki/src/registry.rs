use std::collections::BTreeMap;
use std::path::Path;

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
    std::fs::write(path, format!("{contents}\n")).map_err(|error| WikiError::Io {
        action: "write registry",
        path: Some(path.to_path_buf()),
        source: error.to_string(),
    })
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
}
