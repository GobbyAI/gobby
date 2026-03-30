//! Project identity resolution for gcode standalone mode.
//!
//! Resolution order: .gobby/project.json (gobby) > .gobby/gcode.json (standalone) > generate on-the-fly.
//! gcode never writes to project.json — that's gobby's file.

use std::path::{Path, PathBuf};

use anyhow::Context as _;
use uuid::Uuid;

use crate::models::CODE_INDEX_UUID_NAMESPACE;

/// Walk up from `start` looking for `.gobby/project.json` or `.gobby/gcode.json`.
/// Returns the project root (parent of `.gobby/`) if found.
pub fn find_project_root(start: &Path) -> Option<PathBuf> {
    let mut dir = start;
    loop {
        let gobby_dir = dir.join(".gobby");
        if gobby_dir.join("project.json").exists() || gobby_dir.join("gcode.json").exists() {
            return Some(dir.to_path_buf());
        }
        match dir.parent() {
            Some(parent) => dir = parent,
            None => return None,
        }
    }
}

/// Read project ID from `.gobby/project.json`.
/// Reads `"id"` field first, falls back to `"project_id"` for backwards compat.
pub fn read_project_id(project_root: &Path) -> anyhow::Result<String> {
    let path = project_root.join(".gobby").join("project.json");
    let contents = std::fs::read_to_string(&path)
        .with_context(|| format!("failed to read {}", path.display()))?;
    let json: serde_json::Value = serde_json::from_str(&contents)?;
    json.get("id")
        .or_else(|| json.get("project_id"))
        .and_then(|v| v.as_str())
        .map(String::from)
        .context("'id' field not found in .gobby/project.json")
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

/// Generate a deterministic project ID from the canonical project root path.
/// Uses UUID5 with the same namespace as symbol IDs — key format (bare path)
/// differs from symbol keys so there's no collision risk.
pub fn generate_project_id(project_root: &Path) -> String {
    let canonical = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_path_buf());
    Uuid::new_v5(
        &CODE_INDEX_UUID_NAMESPACE,
        canonical.to_string_lossy().as_bytes(),
    )
    .to_string()
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

    let project_id = generate_project_id(project_root);
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

/// Format current UTC time as ISO 8601 (no chrono dependency).
fn now_iso8601() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};

    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs();
    let micros = dur.subsec_micros();

    let (y, m, d) = days_to_ymd(secs / 86400);
    let daytime = secs % 86400;
    let h = daytime / 3600;
    let min = (daytime % 3600) / 60;
    let s = daytime % 60;

    format!("{y:04}-{m:02}-{d:02}T{h:02}:{min:02}:{s:02}.{micros:06}+00:00")
}

/// Convert days since Unix epoch to (year, month, day).
/// Howard Hinnant's civil_from_days algorithm.
fn days_to_ymd(days: u64) -> (u64, u64, u64) {
    let z = days as i64 + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y as u64, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_generate_project_id_deterministic() {
        let dir = tempfile::tempdir().unwrap();
        let id1 = generate_project_id(dir.path());
        let id2 = generate_project_id(dir.path());
        assert_eq!(id1, id2);
        // Should be valid UUID
        assert!(uuid::Uuid::parse_str(&id1).is_ok());
    }

    #[test]
    fn test_generate_project_id_different_paths() {
        let dir1 = tempfile::tempdir().unwrap();
        let dir2 = tempfile::tempdir().unwrap();
        let id1 = generate_project_id(dir1.path());
        let id2 = generate_project_id(dir2.path());
        assert_ne!(id1, id2);
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
        assert_eq!(id, generate_project_id(dir.path()));
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
    fn test_read_project_id_uses_id_field() {
        let dir = tempfile::tempdir().unwrap();
        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();

        let json = serde_json::json!({
            "id": "correct-id",
            "name": "test"
        });
        std::fs::write(
            gobby_dir.join("project.json"),
            serde_json::to_string(&json).unwrap(),
        )
        .unwrap();

        let id = read_project_id(dir.path()).unwrap();
        assert_eq!(id, "correct-id");
    }

    #[test]
    fn test_read_project_id_falls_back_to_project_id_key() {
        let dir = tempfile::tempdir().unwrap();
        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();

        // Old format with "project_id" instead of "id"
        let json = serde_json::json!({
            "project_id": "legacy-id",
            "name": "test"
        });
        std::fs::write(
            gobby_dir.join("project.json"),
            serde_json::to_string(&json).unwrap(),
        )
        .unwrap();

        let id = read_project_id(dir.path()).unwrap();
        assert_eq!(id, "legacy-id");
    }

    #[test]
    fn test_find_project_root_finds_project_json() {
        let dir = tempfile::tempdir().unwrap();
        let nested = dir.path().join("a").join("b").join("c");
        std::fs::create_dir_all(&nested).unwrap();

        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();
        std::fs::write(gobby_dir.join("project.json"), "{}").unwrap();

        let found = find_project_root(&nested);
        assert_eq!(found, Some(dir.path().to_path_buf()));
    }

    #[test]
    fn test_find_project_root_finds_gcode_json() {
        let dir = tempfile::tempdir().unwrap();
        let nested = dir.path().join("a").join("b");
        std::fs::create_dir_all(&nested).unwrap();

        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();
        std::fs::write(gobby_dir.join("gcode.json"), "{}").unwrap();

        let found = find_project_root(&nested);
        assert_eq!(found, Some(dir.path().to_path_buf()));
    }

    #[test]
    fn test_find_project_root_returns_none() {
        let dir = tempfile::tempdir().unwrap();
        let found = find_project_root(dir.path());
        assert!(found.is_none());
    }

    #[test]
    fn test_now_iso8601_format() {
        let ts = now_iso8601();
        // Should match YYYY-MM-DDTHH:MM:SS.ffffff+00:00
        assert!(ts.len() >= 30, "timestamp too short: {ts}");
        assert!(ts.ends_with("+00:00"));
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
