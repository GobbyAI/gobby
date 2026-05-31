//! Project-root discovery and project-id reading.
//!
//! Kept in lockstep with `gcode/src/project.rs` until PR 4 (R2-08) migrates
//! gcode to import from here.

use anyhow::Context;
use std::path::{Path, PathBuf};

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
        match dir.parent() {
            Some(parent) => dir = parent,
            None => return None,
        }
    }
}

/// Read the project id from `.gobby/project.json`, falling back to
/// `.gobby/gcode.json` for standalone code-index roots. Accepts either `id` or
/// `project_id` as the key.
pub fn read_project_id(project_root: &Path) -> anyhow::Result<String> {
    let project_json = project_root.join(".gobby").join("project.json");
    if project_json.exists() {
        return read_project_id_from(&project_json);
    }

    let gcode_json = project_root.join(".gobby").join("gcode.json");
    read_project_id_from(&gcode_json)
}

fn read_project_id_from(path: &Path) -> anyhow::Result<String> {
    let contents = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read {}", path.display()))?;
    let json: serde_json::Value = serde_json::from_str(&contents)?;
    json.get("id")
        .or_else(|| json.get("project_id"))
        .and_then(|v| v.as_str())
        .map(String::from)
        .with_context(|| format!("'id' or 'project_id' field not found in {}", path.display()))
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
    fn missing_project_id_error_mentions_all_accepted_keys() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let gobby_dir = tmp.path().join(".gobby");
        fs::create_dir(&gobby_dir).expect("create .gobby");
        fs::write(gobby_dir.join("project.json"), r#"{"name":"example"}"#)
            .expect("write project json");

        let error = read_project_id(tmp.path()).expect_err("project id is missing");

        assert!(error.to_string().contains("'id' or 'project_id'"));
    }
}
