//! Project identity helpers shared with gobby-core.
//!
//! gcode never writes identity files: `.gobby/project.json` is gobby's, and
//! primary index writes are fenced on the checkout `gobby init` registers.

use std::path::Path;

// Overlay identity is shared with the grant handshake so the overlay a worktree
// indexes under is, by construction, the overlay its interactive principal binds.
pub use gobby_core::project::{code_index_id_for_root, read_isolation_marker};

/// Check whether any identity file exists for this project root.
pub fn has_identity_file(project_root: &Path) -> bool {
    let gobby_dir = project_root.join(".gobby");
    gobby_dir.join("project.json").exists() || gobby_dir.join("gcode.json").exists()
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
                "id": "copied-parent-id"
            })
            .to_string(),
        )
        .unwrap();
        std::fs::write(
            gobby_dir.join("isolation.json"),
            serde_json::json!({
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
    fn test_has_identity_file() {
        let dir = tempfile::tempdir().unwrap();
        assert!(!has_identity_file(dir.path()));

        let gobby_dir = dir.path().join(".gobby");
        std::fs::create_dir_all(&gobby_dir).unwrap();
        std::fs::write(gobby_dir.join("gcode.json"), "{}").unwrap();
        assert!(has_identity_file(dir.path()));
    }
}
