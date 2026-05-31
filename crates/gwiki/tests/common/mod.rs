#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};

pub const PROJECT_ID: &str = "project-123";
pub const PROJECT_JSON: &str = r#"{
  "id": "project-123",
  "name": "demo"
}
"#;
pub const GCODE_JSON: &str = r#"{
  "id": "project-123",
  "name": "gcode-fixture"
}
"#;

pub fn write_project_json(project: &Path) -> PathBuf {
    write_gobby_fixture(project, "project.json", PROJECT_JSON)
}

pub fn write_gcode_json(project: &Path) -> PathBuf {
    write_gobby_fixture(project, "gcode.json", GCODE_JSON)
}

fn write_gobby_fixture(project: &Path, file_name: &str, contents: &str) -> PathBuf {
    let gobby_dir = project.join(".gobby");
    fs::create_dir_all(&gobby_dir).expect("create .gobby");
    let path = gobby_dir.join(file_name);
    fs::write(&path, contents).expect("write .gobby fixture");
    path
}

pub fn assert_project_json_unchanged(path: &Path) {
    assert_eq!(
        fs::read_to_string(path).expect("read project json"),
        PROJECT_JSON
    );
}
