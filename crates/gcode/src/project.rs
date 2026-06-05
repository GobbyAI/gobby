//! Project identity resolution for gcode standalone mode.
//!
//! Resolution order: .gobby/project.json (gobby) > .gobby/gcode.json (gcode-owned identity) > generate on-the-fly.
//! gcode never writes to project.json — that's gobby's file.

use std::path::{Path, PathBuf};

use anyhow::Context as _;
use gobby_core::project::read_project_id;
use uuid::Uuid;

use crate::models::CODE_INDEX_UUID_NAMESPACE;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IsolationMarker {
    pub parent_project_path: Option<String>,
    pub parent_project_id: Option<String>,
}

/// Read project ID from `.gobby/gcode.json`.
pub fn read_gcode_json(project_root: &Path) -> anyhow::Result<String> {
    let path = project_root.join(".gobby").join("gcode.json");
    let contents = std::fs::read_to_string(&path)
        .with_context(|| format!("failed to read {}", path.display()))?;
    let json: serde_json::Value = serde_json::from_str(&contents)?;
    json.get("id")
        .and_then(|v| v.as_str())
        .map(String::from)
        .context("'id' field not found in .gobby/gcode.json")
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

/// Ensure a gcode identity file exists. Non-destructive:
/// - If `project.json` exists, reads its ID (gobby owns this project)
/// - If `gcode.json` exists, reads its ID
/// - If neither exists, creates `gcode.json`
///
/// Returns `(project_id, was_created)`.
pub fn ensure_gcode_json(project_root: &Path) -> anyhow::Result<(String, bool)> {
    // Gobby's file takes priority
    let project_json = project_root.join(".gobby").join("project.json");
    if project_json.exists() {
        return Ok((read_project_id(project_root)?, false));
    }

    // Already initialized by gcode
    let gcode_json = project_root.join(".gobby").join("gcode.json");
    if gcode_json.exists() {
        return Ok((read_gcode_json(project_root)?, false));
    }

    // Create .gobby/ directory and gcode.json
    let gobby_dir = project_root.join(".gobby");
    std::fs::create_dir_all(&gobby_dir)
        .with_context(|| format!("failed to create {}", gobby_dir.display()))?;

    let project_id = code_index_id_for_root(project_root);
    let project_name = project_root
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "unknown".to_string());

    let created_at = now_iso8601();

    let content = serde_json::json!({
        "id": project_id,
        "name": project_name,
        "created_at": created_at
    });

    let json_str = serde_json::to_string_pretty(&content)?;
    std::fs::write(&gcode_json, &json_str)
        .with_context(|| format!("failed to write {}", gcode_json.display()))?;

    Ok((project_id, true))
}

/// Check whether any identity file exists for this project root.
pub fn has_identity_file(project_root: &Path) -> bool {
    let gobby_dir = project_root.join(".gobby");
    gobby_dir.join("project.json").exists() || gobby_dir.join("gcode.json").exists()
}

// ── Internal helpers ────────────────────────────────────────────────

/// Format current UTC time as ISO 8601.
fn now_iso8601() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

fn absolute_fallback(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| std::env::temp_dir())
            .join(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_code_index_id_for_root_deterministic() {
        let dir = tempfile::tempdir().unwrap();
        let id1 = code_index_id_for_root(dir.path());
        let id2 = code_index_id_for_root(dir.path());
        assert_eq!(id1, id2);
        // Should be valid UUID
        assert!(uuid::Uuid::parse_str(&id1).is_ok());
    }

    #[test]
    fn test_code_index_id_for_root_different_paths() {
        let dir1 = tempfile::tempdir().unwrap();
        let dir2 = tempfile::tempdir().unwrap();
        let id1 = code_index_id_for_root(dir1.path());
        let id2 = code_index_id_for_root(dir2.path());
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_read_isolation_marker_detects_parent_fields() {
        let dir = tempfile::tempdir().unwrap();
        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();
        std::fs::write(
            gobby_dir.join("project.json"),
            serde_json::json!({
                "id": "copied-parent-id",
                "parent_project_path": "/parent/root",
                "parent_project_id": "parent-id"
            })
            .to_string(),
        )
        .unwrap();

        let marker = read_isolation_marker(dir.path()).expect("isolation marker");

        assert_eq!(marker.parent_project_path.as_deref(), Some("/parent/root"));
        assert_eq!(marker.parent_project_id.as_deref(), Some("parent-id"));
    }

    #[test]
    fn test_ensure_gcode_json_creates_new() {
        let dir = tempfile::tempdir().unwrap();
        let (id, created) = ensure_gcode_json(dir.path()).unwrap();
        assert!(created);
        assert!(uuid::Uuid::parse_str(&id).is_ok());

        // Verify file exists with correct content
        let path = dir.path().join(".gobby").join("gcode.json");
        assert!(path.exists());
        let contents: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(contents["id"].as_str().unwrap(), id);

        // ID should match deterministic generation
        assert_eq!(id, code_index_id_for_root(dir.path()));
    }

    #[test]
    fn test_ensure_gcode_json_skips_when_project_json_exists() {
        let dir = tempfile::tempdir().unwrap();
        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();

        // Write a gobby project.json
        let project_json = serde_json::json!({
            "id": "gobby-owned-id-123",
            "name": "test-project"
        });
        std::fs::write(
            gobby_dir.join("project.json"),
            serde_json::to_string_pretty(&project_json).unwrap(),
        )
        .unwrap();

        let (id, created) = ensure_gcode_json(dir.path()).unwrap();
        assert!(!created);
        assert_eq!(id, "gobby-owned-id-123");

        // gcode.json should NOT exist
        assert!(!gobby_dir.join("gcode.json").exists());
    }

    #[test]
    fn test_ensure_gcode_json_reads_existing() {
        let dir = tempfile::tempdir().unwrap();

        // Create gcode.json first
        let (id1, created1) = ensure_gcode_json(dir.path()).unwrap();
        assert!(created1);

        // Second call should read, not overwrite
        let original_bytes = std::fs::read(dir.path().join(".gobby").join("gcode.json")).unwrap();
        let (id2, created2) = ensure_gcode_json(dir.path()).unwrap();
        assert!(!created2);
        assert_eq!(id1, id2);

        // File should be byte-identical
        let after_bytes = std::fs::read(dir.path().join(".gobby").join("gcode.json")).unwrap();
        assert_eq!(original_bytes, after_bytes);
    }

    #[test]
    fn test_now_iso8601_format() {
        let ts = now_iso8601();
        // Should match YYYY-MM-DDTHH:MM:SS.ffffffZ
        assert!(ts.len() >= 27, "timestamp too short: {ts}");
        assert!(ts.ends_with('Z'));
        assert!(ts.contains('T'));
    }

    #[test]
    fn test_has_identity_file() {
        let dir = tempfile::tempdir().unwrap();
        assert!(!has_identity_file(dir.path()));

        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();
        std::fs::write(gobby_dir.join("gcode.json"), "{}").unwrap();
        assert!(has_identity_file(dir.path()));
    }
}
