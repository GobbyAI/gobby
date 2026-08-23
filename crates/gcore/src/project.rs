//! Project-root discovery and project-id reading.
//!
//! Shared helpers for crates that read Gobby project metadata without mutating it.

use anyhow::Context;
use std::path::{Path, PathBuf};
use uuid::Uuid;

/// Canonical personal-project id. Matches the Python `PERSONAL_PROJECT_ID`.
pub const PERSONAL_PROJECT_ID: &str = "00000000-0000-0000-0000-000000060887";

/// Stable namespace for deterministic code-index UUIDs (symbols and overlay
/// project ids). Must match Python `CODE_INDEX_UUID_NAMESPACE` and the SQL
/// `gobby_agent_auth.code_index_project_id` helper:
/// `uuid.UUID("c0de1de0-0000-4000-8000-000000000000")`.
pub const CODE_INDEX_UUID_NAMESPACE: Uuid = Uuid::from_bytes([
    0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
]);

/// Parent linkage written into a worktree's or clone's `.gobby/project.json`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IsolationMarker {
    pub parent_project_path: Option<String>,
    pub parent_project_id: Option<String>,
}

/// Walk up from `start` looking for a `.gobby` directory containing either
/// `project.json` or `gcode.json`. Returns the project root (the directory
/// containing `.gobby`) or `None` if no project is found before hitting the
/// filesystem root.
pub fn find_project_root(start: &Path) -> Option<PathBuf> {
    let mut dir = start;
    loop {
        let gobby_dir = dir.join(".gobby");
        if gobby_dir.join("project.json").exists() || gobby_dir.join("gcode.json").exists() {
            return Some(dir.to_path_buf());
        }
        dir = dir.parent()?;
    }
}

/// Read the project id from `.gobby/project.json`, falling back to
/// `.gobby/gcode.json` for standalone code-index roots.
pub fn read_project_id(project_root: &Path) -> anyhow::Result<String> {
    let project_json = project_root.join(".gobby").join("project.json");
    if project_json.exists() {
        match read_project_id_from(&project_json) {
            Ok(project_id) => return Ok(project_id),
            Err(project_error) => {
                let gcode_json = project_root.join(".gobby").join("gcode.json");
                if !gcode_json.exists() {
                    return Err(project_error);
                }
                return read_project_id_from(&gcode_json).with_context(|| {
                    format!(
                        "failed to read project id from {} and fallback {}",
                        project_json.display(),
                        gcode_json.display()
                    )
                });
            }
        }
    }

    let gcode_json = project_root.join(".gobby").join("gcode.json");
    read_project_id_from(&gcode_json)
}

/// Generate a deterministic code-index ID from the canonical project root path.
/// Uses UUID5 with the same namespace as symbol IDs — key format (bare path)
/// differs from symbol keys so there's no collision risk.
pub fn code_index_id_for_root(root: &Path) -> String {
    let canonical = root
        .canonicalize()
        .unwrap_or_else(|_| absolute_fallback(root));
    Uuid::new_v5(
        &CODE_INDEX_UUID_NAMESPACE,
        canonical.to_string_lossy().as_bytes(),
    )
    .to_string()
}

/// Read the isolated-root marker from `.gobby/project.json`, if present.
pub fn read_isolation_marker(project_root: &Path) -> Option<IsolationMarker> {
    let path = project_root.join(".gobby").join("project.json");
    let contents = std::fs::read_to_string(path).ok()?;
    let json: serde_json::Value = serde_json::from_str(&contents).ok()?;
    let parent_project_path = json
        .get("parent_project_path")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned);
    let parent_project_id = json
        .get("parent_project_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned);

    if parent_project_path.is_some() || parent_project_id.is_some() {
        Some(IsolationMarker {
            parent_project_path,
            parent_project_id,
        })
    } else {
        None
    }
}

/// Resolve a marker's `parent_project_path` relative to the isolated root.
pub fn resolve_parent_project_root(root: &Path, parent_project_path: &str) -> PathBuf {
    let parent = PathBuf::from(parent_project_path);
    let parent = if parent.is_absolute() {
        parent
    } else {
        root.join(parent)
    };
    parent.canonicalize().unwrap_or(parent)
}

/// A marker whose parent resolves to the root itself describes a main checkout
/// that was once registered as its own parent, not an overlay.
pub fn is_self_referential_isolation_marker(marker: &IsolationMarker, root: &Path) -> bool {
    let Some(parent_project_path) = marker.parent_project_path.as_deref() else {
        return false;
    };
    resolve_parent_project_root(root, parent_project_path) == root
}

/// The code-index overlay id a caller working in `project_root` must bind to.
///
/// `Some` only when the root carries a complete, non-self-referential isolation
/// marker; the main checkout and malformed markers yield `None`, so the caller
/// falls back to the parent project scope. The value is the same
/// `code_index_id_for_root` the indexer uses for `ProjectIndexScope::Overlay`.
pub fn code_overlay_project_id(project_root: &Path) -> Option<String> {
    let root = project_root
        .canonicalize()
        .unwrap_or_else(|_| absolute_fallback(project_root));
    let marker = read_isolation_marker(&root)?;
    if marker.parent_project_path.is_none() || marker.parent_project_id.is_none() {
        return None;
    }
    if is_self_referential_isolation_marker(&marker, &root) {
        return None;
    }
    Some(code_index_id_for_root(&root))
}

/// Absolute form of `path` for identity hashing when canonicalization fails.
pub fn absolute_fallback(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| std::env::temp_dir())
            .join(path)
    }
}

fn read_project_id_from(path: &Path) -> anyhow::Result<String> {
    let contents = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read {}", path.display()))?;
    let json: serde_json::Value = serde_json::from_str(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    json.get("id")
        .and_then(|v| v.as_str())
        .map(String::from)
        .with_context(|| format!("'id' field not found in {}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn read_project_id_is_non_destructive() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let gobby_dir = tmp.path().join(".gobby");
        fs::create_dir(&gobby_dir).expect("create .gobby");
        let project_json = gobby_dir.join("project.json");
        let contents = r#"{
  "id": "project-id",
  "name": "example"
}
"#;
        fs::write(&project_json, contents).expect("write project json");

        let project_id = read_project_id(tmp.path()).expect("read project id");

        assert_eq!(project_id, "project-id");
        assert_eq!(
            fs::read_to_string(&project_json).expect("read project json"),
            contents
        );
    }

    #[test]
    fn read_project_id_falls_back_to_gcode_json_root_marker() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let nested = tmp.path().join("src").join("bin");
        let gobby_dir = tmp.path().join(".gobby");
        fs::create_dir(&gobby_dir).expect("create .gobby");
        fs::create_dir_all(&nested).expect("create nested");
        fs::write(
            gobby_dir.join("gcode.json"),
            r#"{
  "id": "standalone-code-index",
  "name": "example"
}
"#,
        )
        .expect("write gcode json");

        assert_eq!(find_project_root(&nested).as_deref(), Some(tmp.path()));
        assert_eq!(
            read_project_id(tmp.path()).expect("read gcode project id"),
            "standalone-code-index"
        );
    }

    #[test]
    fn overlay_requires_a_complete_foreign_isolation_marker() {
        let parent = tempfile::tempdir().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let gobby = dir.path().join(".gobby");
        fs::create_dir_all(&gobby).unwrap();
        let marker = gobby.join("project.json");

        fs::write(&marker, r#"{"id": "parent-id"}"#).unwrap();
        assert_eq!(code_overlay_project_id(dir.path()), None);

        fs::write(
            &marker,
            r#"{"id": "parent-id", "parent_project_id": "parent-id"}"#,
        )
        .unwrap();
        assert_eq!(code_overlay_project_id(dir.path()), None, "half marker");

        fs::write(
            &marker,
            format!(
                r#"{{"id": "parent-id", "parent_project_path": "{}", "parent_project_id": "parent-id"}}"#,
                dir.path().display()
            ),
        )
        .unwrap();
        assert_eq!(
            code_overlay_project_id(dir.path()),
            None,
            "self-referential marker"
        );

        fs::write(
            &marker,
            format!(
                r#"{{"id": "parent-id", "parent_project_path": "{}", "parent_project_id": "parent-id"}}"#,
                parent.path().display()
            ),
        )
        .unwrap();
        assert_eq!(
            code_overlay_project_id(dir.path()),
            Some(code_index_id_for_root(dir.path()))
        );
        assert_eq!(read_project_id(dir.path()).unwrap(), "parent-id");
    }

    #[test]
    fn missing_project_id_error_mentions_id_key() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let gobby_dir = tmp.path().join(".gobby");
        fs::create_dir(&gobby_dir).expect("create .gobby");
        fs::write(gobby_dir.join("project.json"), r#"{"name":"example"}"#)
            .expect("write project json");

        let error = read_project_id(tmp.path()).expect_err("project id is missing");

        assert!(error.to_string().contains("'id' field not found"));
    }

    #[test]
    fn read_project_id_falls_back_to_gcode_json_when_project_json_is_bad() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let gobby_dir = tmp.path().join(".gobby");
        fs::create_dir(&gobby_dir).expect("create .gobby");
        fs::write(gobby_dir.join("project.json"), r#"{"name":"example"}"#)
            .expect("write project json");
        fs::write(
            gobby_dir.join("gcode.json"),
            r#"{"id":"standalone-fallback"}"#,
        )
        .expect("write gcode json");

        assert_eq!(
            read_project_id(tmp.path()).expect("read fallback project id"),
            "standalone-fallback"
        );
    }

    #[test]
    fn read_project_id_falls_back_to_gcode_json_when_project_json_is_malformed() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let gobby_dir = tmp.path().join(".gobby");
        fs::create_dir(&gobby_dir).expect("create .gobby");
        fs::write(gobby_dir.join("project.json"), r#"{"id":"broken""#)
            .expect("write malformed project json");
        fs::write(
            gobby_dir.join("gcode.json"),
            r#"{"id":"standalone-fallback"}"#,
        )
        .expect("write gcode json");

        assert_eq!(
            read_project_id(tmp.path()).expect("read fallback project id"),
            "standalone-fallback"
        );
    }
}
