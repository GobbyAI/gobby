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
        Self::project_with_registry(project_id, project_root, root, registry_path)
    }

    pub fn project_with_registry(
        project_id: String,
        project_root: PathBuf,
        root: PathBuf,
        registry_path: PathBuf,
    ) -> Self {
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
    let root = hub.join(&topic);

    Ok(ResolvedScope::topic(topic, root, hub.join("wikis.json")))
}

fn resolve_project_from_root(project_root: &Path) -> Result<ResolvedScope, WikiError> {
    if let Some(scope) = resolve_personal_from_root(project_root)? {
        return Ok(scope);
    }
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
    let root = gobby_core::vault::resolve_vault_dir(&project_root).ok_or_else(|| {
        WikiError::InvalidScope {
            detail: format!(
                "no usable wiki vault directory under {}: `{}` and every `{}` fallback is occupied by a non-vault path",
                project_root.display(),
                gobby_core::vault::DEFAULT_VAULT_DIR,
                gobby_core::vault::FALLBACK_VAULT_DIR,
            ),
        }
    })?;

    Ok(ResolvedScope::project(project_id, project_root, root))
}

fn resolve_personal_from_root(project_root: &Path) -> Result<Option<ResolvedScope>, WikiError> {
    let view =
        gobby_core::bootstrap::read_files_home_view().map_err(|error| WikiError::Config {
            detail: error.to_string(),
        })?;
    let Some(files_home) = view.files_home else {
        return Ok(None);
    };
    let personal_root = files_home.join("_personal");
    if !paths_match(project_root, &personal_root) {
        return Ok(None);
    }
    let wiki_home = files_home.join("wiki");
    Ok(Some(ResolvedScope::project_with_registry(
        gobby_core::project::PERSONAL_PROJECT_ID.to_string(),
        personal_root,
        wiki_home.join("personal"),
        wiki_home.join("wikis.json"),
    )))
}

fn resolve_hub_path(source: &mut impl ConfigSource) -> Result<PathBuf, WikiError> {
    let view =
        gobby_core::bootstrap::read_files_home_view().map_err(|error| WikiError::Config {
            detail: error.to_string(),
        })?;
    if view.datastore_mode == gobby_core::bootstrap::DatastoreMode::Remote
        && view.hub_daemon_url.is_some()
    {
        return Err(WikiError::Config {
            detail: "topic/personal filesystem resolution is not available on a remote-mode daemon"
                .to_string(),
        });
    }
    if let Some(files_home) = view.files_home {
        let wiki_home = files_home.join("wiki");
        if let Some(override_path) = configured_hub_override(source)?
            && !paths_match(&override_path, &wiki_home)
        {
            return Err(WikiError::Config {
                detail: "GOBBY_WIKI_HUB does not match files_home/wiki".to_string(),
            });
        }
        return Ok(wiki_home);
    }
    if let Some(override_path) = configured_hub_override(source)? {
        return Ok(override_path);
    }
    default_hub_path()
}

fn configured_hub_override(source: &mut impl ConfigSource) -> Result<Option<PathBuf>, WikiError> {
    if let Some(path) = std::env::var_os(HUB_ENV).filter(|value| !value.is_empty()) {
        let path = PathBuf::from(path);
        if let Some(value) = path.to_str()
            && (value == "~" || value.starts_with("~/"))
        {
            return expand_home(value).map(Some);
        }
        return Ok(Some(path));
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
            return expand_home(value.trim()).map(Some);
        }
    }
    Ok(None)
}

fn default_hub_path() -> Result<PathBuf, WikiError> {
    Err(WikiError::Config {
        detail: "configure GOBBY_WIKI_HUB or run on the files owner".to_string(),
    })
}

fn paths_match(left: &Path, right: &Path) -> bool {
    match (left.canonicalize(), right.canonicalize()) {
        (Ok(left), Ok(right)) => left == right,
        _ => normalize_abs(left) == normalize_abs(right),
    }
}

fn normalize_abs(path: &Path) -> PathBuf {
    if path.is_absolute() {
        return path.components().collect();
    }
    std::env::current_dir()
        .map(|cwd| cwd.join(path).components().collect())
        .unwrap_or_else(|_| path.to_path_buf())
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
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path().join("gobby-home");
        fs::create_dir_all(&home).expect("gobby home");
        let _env = EnvGuard::unset(HUB_ENV).and_set("GOBBY_HOME", home.as_os_str());
        let hub = tmp.path().join("knowledge");
        let mut config = TestConfig::with("wiki.hub_path", hub.display().to_string());

        let scope = resolve_with_source(
            &crate::ScopeSelection::topic("rust-async"),
            tmp.path(),
            &mut config,
        )
        .expect("topic scope resolves");

        assert_eq!(scope.identity(), "topic:rust-async");
        assert_eq!(scope.root(), hub.join("rust-async"));
        assert_eq!(scope.registry_path(), hub.join("wikis.json"));
        assert_ne!(scope.root(), hub.join("topics").join("rust-async"));
    }

    #[test]
    fn rejects_invalid_topic_names() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let hub = tmp.path().join("knowledge");
        for topic in [
            ".",
            "..",
            "bad/topic",
            r"bad\topic",
            "bad:topic",
            "personal",
            "_personal",
            "wiki",
        ] {
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
        assert_eq!(scope.root(), canonical_project.join("wiki"));
        assert_eq!(
            fs::read_to_string(gcode_json).expect("read gcode json"),
            original_gcode_json
        );
        assert!(
            !project.join("wiki").exists(),
            "resolution must not initialize the vault"
        );
    }

    #[test]
    fn project_scope_falls_back_when_wiki_is_occupied_by_non_vault() {
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
        fs::create_dir_all(project.join("wiki")).expect("create non-vault wiki collision");

        let mut config = TestConfig::with(
            "wiki.hub_path",
            tmp.path().join("hub").display().to_string(),
        );
        let scope = resolve_with_source(
            &crate::ScopeSelection::project(&project),
            &project,
            &mut config,
        )
        .expect("project scope resolves");
        let canonical_project = project.canonicalize().expect("canonicalize project root");

        assert_eq!(scope.root(), canonical_project.join("gobby-wiki"));
    }

    #[test]
    fn project_scope_prefers_existing_wiki_vault() {
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
        let state_dir = project.join("wiki").join(gobby_core::vault::STATE_ROOT);
        fs::create_dir_all(&state_dir).expect("create vault state dir");
        fs::write(state_dir.join(gobby_core::vault::SCOPE_FILE), "{}\n").expect("mark vault");

        let mut config = TestConfig::with(
            "wiki.hub_path",
            tmp.path().join("hub").display().to_string(),
        );
        let scope = resolve_with_source(
            &crate::ScopeSelection::project(&project),
            &project,
            &mut config,
        )
        .expect("project scope resolves");
        let canonical_project = project.canonicalize().expect("canonicalize project root");

        assert_eq!(scope.root(), canonical_project.join("wiki"));
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
        assert_eq!(scope.root(), project.join("wiki"));
        assert!(scope.root().is_absolute());
    }

    #[test]
    #[serial_test::serial]
    fn present_local_bootstrap_uses_files_home_wiki() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path().join("gobby-home");
        let files_home = tmp.path().join("files");
        fs::create_dir_all(&home).expect("home");
        fs::create_dir_all(&files_home).expect("files");
        fs::write(
            home.join("bootstrap.yaml"),
            format!(
                "datastore_mode: local\nfiles_home: {}\ndaemon_port: 60887\nbind_host: 127.0.0.1\n",
                files_home.display()
            ),
        )
        .expect("bootstrap");
        let _env = EnvGuard::unset(HUB_ENV).and_set("GOBBY_HOME", home.as_os_str());
        let mut config = TestConfig::with(
            "wiki.hub_path",
            tmp.path().join("other").display().to_string(),
        );
        let err = resolve_with_source(
            &crate::ScopeSelection::topic("foo"),
            tmp.path(),
            &mut config,
        )
        .expect_err("mismatched override refuses");
        assert!(matches!(err, WikiError::Config { .. }));

        let mut matching = TestConfig::with(
            "wiki.hub_path",
            files_home.join("wiki").display().to_string(),
        );
        let scope = resolve_with_source(
            &crate::ScopeSelection::topic("foo"),
            tmp.path(),
            &mut matching,
        )
        .expect("matching override");
        assert_eq!(scope.root(), files_home.join("wiki").join("foo"));
    }

    #[test]
    #[serial_test::serial]
    fn remote_bootstrap_refuses_topic_resolution() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path().join("gobby-home");
        fs::create_dir_all(&home).expect("home");
        fs::write(
            home.join("bootstrap.yaml"),
            "datastore_mode: remote\nhub_daemon_url: https://hub.example.test:7443\n",
        )
        .expect("bootstrap");
        let hub = tmp.path().join("override");
        let _env = EnvGuard::set("GOBBY_HOME", home.as_os_str()).and_set(HUB_ENV, hub.as_os_str());
        let mut config = TestConfig::with("wiki.hub_path", hub.display().to_string());
        let err = resolve_with_source(
            &crate::ScopeSelection::topic("foo"),
            tmp.path(),
            &mut config,
        )
        .expect_err("remote refuses");
        assert!(matches!(err, WikiError::Config { .. }));
    }

    #[test]
    #[serial_test::serial]
    fn personal_root_maps_to_wiki_personal() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path().join("gobby-home");
        let files_home = tmp.path().join("files");
        let personal = files_home.join("_personal");
        fs::create_dir_all(&home).expect("home");
        fs::create_dir_all(&personal).expect("personal");
        fs::write(
            home.join("bootstrap.yaml"),
            format!(
                "datastore_mode: local\nfiles_home: {}\ndaemon_port: 60887\nbind_host: 127.0.0.1\n",
                files_home.display()
            ),
        )
        .expect("bootstrap");
        let _env = EnvGuard::unset(HUB_ENV).and_set("GOBBY_HOME", home.as_os_str());
        let mut config = TestConfig::with(
            "wiki.hub_path",
            tmp.path().join("hub").display().to_string(),
        );
        let scope = resolve_with_source(
            &crate::ScopeSelection::project(&personal),
            tmp.path(),
            &mut config,
        )
        .expect("personal root");
        assert_eq!(
            scope.project_id(),
            Some(gobby_core::project::PERSONAL_PROJECT_ID)
        );
        assert_eq!(scope.root(), files_home.join("wiki").join("personal"));
        assert_eq!(
            scope.registry_path(),
            files_home.join("wiki").join("wikis.json")
        );
    }
}
