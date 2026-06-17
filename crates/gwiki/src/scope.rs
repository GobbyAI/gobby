use std::path::{Path, PathBuf};

use gobby_core::config::{ConfigSource, EnvOnlySource};

use crate::models::{validate_project_id, validate_topic_name};
use crate::{ScopeSelection, WikiError};

const HUB_ENV: &str = "GOBBY_WIKI_HUB";
const HUB_CONFIG_KEYS: [&str; 2] = ["wiki.hub_path", "gwiki.hub_path"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedScope {
    kind: ScopeKind,
    root: PathBuf,
    registry_path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScopeKind {
    Topic {
        name: String,
    },
    Project {
        project_id: String,
        project_root: PathBuf,
    },
}

impl ResolvedScope {
    pub fn topic(name: String, root: PathBuf, registry_path: PathBuf) -> Self {
        Self {
            kind: ScopeKind::Topic { name },
            root,
            registry_path,
        }
    }

    pub fn project(project_id: String, project_root: PathBuf, root: PathBuf) -> Self {
        let registry_path = root.join("wikis.json");
        Self {
            kind: ScopeKind::Project {
                project_id,
                project_root,
            },
            root,
            registry_path,
        }
    }

    pub fn kind(&self) -> &ScopeKind {
        &self.kind
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn registry_path(&self) -> &Path {
        &self.registry_path
    }

    pub fn identity(&self) -> String {
        match &self.kind {
            ScopeKind::Topic { name } => format!("topic:{name}"),
            ScopeKind::Project { project_id, .. } => format!("project:{project_id}"),
        }
    }

    pub fn topic_name(&self) -> Option<&str> {
        match &self.kind {
            ScopeKind::Topic { name } => Some(name),
            ScopeKind::Project { .. } => None,
        }
    }

    pub fn project_id(&self) -> Option<&str> {
        match &self.kind {
            ScopeKind::Topic { .. } => None,
            ScopeKind::Project { project_id, .. } => Some(project_id),
        }
    }

    pub fn project_root(&self) -> Option<&Path> {
        match &self.kind {
            ScopeKind::Topic { .. } => None,
            ScopeKind::Project { project_root, .. } => Some(project_root),
        }
    }
}

pub fn resolve(selection: &ScopeSelection, cwd: &Path) -> Result<ResolvedScope, WikiError> {
    let mut source = EnvOnlySource;
    resolve_with_source(selection, cwd, &mut source)
}

pub fn resolve_with_source(
    selection: &ScopeSelection,
    cwd: &Path,
    source: &mut impl ConfigSource,
) -> Result<ResolvedScope, WikiError> {
    if let Some(topic) = selection.topic_name() {
        return resolve_topic(topic, source);
    }

    if let Some(project_root) = selection.project_root() {
        let project_root = if project_root.is_relative() {
            cwd.join(project_root)
        } else {
            project_root.to_path_buf()
        };
        return resolve_project_from_root(&project_root);
    }

    if let Some(project_root) = gobby_core::project::find_project_root(cwd) {
        return resolve_project_from_root(&project_root);
    }

    Err(WikiError::InvalidScope {
        detail: "select a wiki scope with --topic <name> or run inside a Gobby project".to_string(),
    })
}

fn resolve_topic(topic: &str, source: &mut impl ConfigSource) -> Result<ResolvedScope, WikiError> {
    let topic = validate_topic_name(topic)?;
    let hub = resolve_hub_path(source)?;
    let root = hub.join("topics").join(&topic);

    Ok(ResolvedScope::topic(topic, root, hub.join("wikis.json")))
}

fn resolve_project_from_root(project_root: &Path) -> Result<ResolvedScope, WikiError> {
    let project_root = project_root
        .canonicalize()
        .map_err(|error| WikiError::InvalidScope {
            detail: format!(
                "failed to resolve project root {}: {error}",
                project_root.display()
            ),
        })?;
    let project_id = gobby_core::project::read_project_id(&project_root).map_err(|error| {
        WikiError::InvalidScope {
            detail: format!(
                "failed to read project identity from {}: {error}",
                project_root.display()
            ),
        }
    })?;
    let project_id = validate_project_id(&project_id)?;
    let root = project_root.join("gobby-wiki");

    Ok(ResolvedScope::project(project_id, project_root, root))
}

fn resolve_hub_path(source: &mut impl ConfigSource) -> Result<PathBuf, WikiError> {
    if let Some(path) = std::env::var_os(HUB_ENV).filter(|value| !value.is_empty()) {
        let path = PathBuf::from(path);
        if let Some(value) = path.to_str()
            && (value == "~" || value.starts_with("~/"))
        {
            return expand_home(value);
        }
        return Ok(path);
    }

    for key in HUB_CONFIG_KEYS {
        let Some(value) = source.config_value(key) else {
            continue;
        };
        let value = source
            .resolve_value(&value)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve {key}: {error}"),
            })?;
        if !value.trim().is_empty() {
            return expand_home(value.trim());
        }
    }

    default_hub_path()
}

fn default_hub_path() -> Result<PathBuf, WikiError> {
    let home = dirs::home_dir().ok_or_else(|| WikiError::Config {
        detail: "HOME is not set; configure GOBBY_WIKI_HUB or wiki.hub_path".to_string(),
    })?;

    Ok(home.join("wiki"))
}

fn expand_home(path: &str) -> Result<PathBuf, WikiError> {
    if path == "~" {
        return dirs::home_dir().ok_or_else(|| WikiError::Config {
            detail: "HOME is not set; cannot expand `~` in wiki hub path".to_string(),
        });
    }

    if let Some(rest) = path.strip_prefix("~/") {
        return dirs::home_dir()
            .map(|home| home.join(rest))
            .ok_or_else(|| WikiError::Config {
                detail: format!("HOME is not set; cannot expand `{path}` in wiki hub path"),
            });
    }

    Ok(PathBuf::from(path))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::support::test_env::EnvGuard;
    use gobby_core::config::ConfigSource;
    use std::collections::HashMap;
    use std::fs;

    struct TestConfig {
        values: HashMap<String, String>,
    }

    impl TestConfig {
        fn with(key: &str, value: impl Into<String>) -> Self {
            Self {
                values: HashMap::from([(key.to_string(), value.into())]),
            }
        }
    }

    impl ConfigSource for TestConfig {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            Ok(value.to_string())
        }
    }

    #[test]
    #[serial_test::serial]
    fn resolves_global_topic() {
        let _env = EnvGuard::unset(HUB_ENV);
        let tmp = tempfile::tempdir().expect("tempdir");
        let hub = tmp.path().join("knowledge");
        let mut config = TestConfig::with("wiki.hub_path", hub.display().to_string());

        let scope = resolve_with_source(
            &crate::ScopeSelection::topic("rust-async"),
            tmp.path(),
            &mut config,
        )
        .expect("topic scope resolves");

        assert_eq!(scope.identity(), "topic:rust-async");
        assert_eq!(scope.root(), hub.join("topics").join("rust-async"));
        assert_eq!(scope.registry_path(), hub.join("wikis.json"));
    }

    #[test]
    fn rejects_invalid_topic_names() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let hub = tmp.path().join("knowledge");
        for topic in [".", "..", "bad/topic", r"bad\topic", "bad:topic"] {
            let mut config = TestConfig::with("wiki.hub_path", hub.display().to_string());
            let err = resolve_with_source(
                &crate::ScopeSelection::topic(topic),
                tmp.path(),
                &mut config,
            )
            .expect_err("invalid topic fails");

            assert!(matches!(err, WikiError::InvalidScope { .. }));
        }
    }

    #[test]
    fn resolves_project_scope_read_only() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let project = tmp.path().join("project");
        let nested = project.join("src").join("bin");
        fs::create_dir_all(project.join(".gobby")).expect("create .gobby");
        fs::create_dir_all(&nested).expect("create nested dir");
        let gcode_json = project.join(".gobby").join("gcode.json");
        let original_gcode_json = r#"{
  "id": "project-123",
  "name": "demo"
}
"#;
        fs::write(&gcode_json, original_gcode_json).expect("write gcode json");

        let mut config = TestConfig::with(
            "wiki.hub_path",
            tmp.path().join("hub").display().to_string(),
        );
        let scope = resolve_with_source(
            &crate::ScopeSelection::project(&project),
            &nested,
            &mut config,
        )
        .expect("project scope resolves");
        let canonical_project = project.canonicalize().expect("canonicalize project root");

        assert_eq!(scope.identity(), "project:project-123");
        assert_eq!(scope.root(), canonical_project.join("gobby-wiki"));
        assert_eq!(
            fs::read_to_string(gcode_json).expect("read gcode json"),
            original_gcode_json
        );
        assert!(
            !project.join("gobby-wiki").exists(),
            "resolution must not initialize the vault"
        );
    }

    #[test]
    fn project_dot_resolves_to_absolute_project_wiki_root() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let project = tmp.path().join("project");
        fs::create_dir_all(project.join(".gobby")).expect("create .gobby");
        fs::write(
            project.join(".gobby").join("gcode.json"),
            r#"{
  "id": "project-123",
  "name": "demo"
}
"#,
        )
        .expect("write gcode json");

        let mut config = TestConfig::with(
            "wiki.hub_path",
            tmp.path().join("hub").display().to_string(),
        );
        let scope =
            resolve_with_source(&crate::ScopeSelection::project("."), &project, &mut config)
                .expect("project scope resolves");
        let project = project.canonicalize().expect("canonicalize project root");

        assert_eq!(scope.project_root(), Some(project.as_path()));
        assert_eq!(scope.root(), project.join("gobby-wiki"));
        assert!(scope.root().is_absolute());
    }
}
